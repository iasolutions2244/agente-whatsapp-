import contextvars
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Any

import requests
from anthropic import Anthropic
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

load_dotenv()

from crypto_utils import encrypt_value, decrypt_value  # noqa: E402 (requiere ENCRYPTION_KEY, cargada arriba con load_dotenv)

for _var in ["ANTHROPIC_API_KEY", "WHATSAPP_TOKEN", "WHATSAPP_PHONE_ID", "WHATSAPP_VERIFY_TOKEN", "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "ADMIN_API_KEY"]:
    logging.info("ENV CHECK | %s = %s", _var, "SET" if os.environ.get(_var) else "*** MISSING ***")

# Fudo "global" queda solo como fallback para el cliente demo / pruebas locales
_FUDO_GLOBAL_ENABLED = bool(os.environ.get("FUDO_API_KEY") and os.environ.get("FUDO_API_SECRET"))
logging.info("FUDO global (fallback) | habilitado=%s", _FUDO_GLOBAL_ENABLED)

app = Flask(__name__)

# ──────────────────────────────────────────────────────────────
# Contexto del cliente actual (multi-tenant)
# ──────────────────────────────────────────────────────────────
# Cada request de WhatsApp puede venir de un restaurante distinto.
# Usamos contextvars para que, dentro de una misma petición, todas las
# funciones de Fudo sepan automáticamente "de qué cliente son los datos"
# sin tener que pasar el cliente_id manualmente en cada llamada de tool_use.

_current_fudo_client: contextvars.ContextVar = contextvars.ContextVar("current_fudo_client", default=None)
_current_cliente_info: contextvars.ContextVar = contextvars.ContextVar("current_cliente_info", default=None)


# ──────────────────────────────────────────────────────────────
# Supabase Client
# ──────────────────────────────────────────────────────────────

_supabase: Client | None = None

def get_supabase() -> Client | None:
    global _supabase
    if _supabase:
        return _supabase
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if url and key:
        _supabase = create_client(url, key)
        logging.info("Supabase conectado")
    else:
        logging.warning("Supabase no configurado — usando memoria RAM")
    return _supabase


# ──────────────────────────────────────────────────────────────
# Funciones de memoria + credenciales por cliente (Supabase)
# ──────────────────────────────────────────────────────────────

def _match_restaurante(mensaje: str, todos_accesos: list[dict]) -> dict | None:
    """Matching conservador: el nombre completo del restaurante debe aparecer literalmente
    en el mensaje. Nombres de 3 caracteres o menos se descartan. Si hay 0 o más de 1
    coincidencia (ambigüedad) devuelve None para que Claude pida aclaración."""
    mensaje_lower = mensaje.lower()
    matches = []
    for cliente in todos_accesos:
        nombre = (cliente.get("nombre_restaurante") or "").strip()
        if len(nombre) <= 3:
            continue
        if nombre.lower() in mensaje_lower:
            matches.append(cliente)
    return matches[0] if len(matches) == 1 else None


def _restaurante_activo_vigente(cliente_info: dict, todos_accesos: list[dict]) -> dict | None:
    """Si el usuario tiene un restaurante_activo_id/ts guardado y de menos de 2 horas,
    devuelve el acceso correspondiente. Si no hay elección previa, expiró, o el acceso
    ya no está entre los activos del usuario, devuelve None."""
    rid = cliente_info.get("_restaurante_activo_id")
    ts_raw = cliente_info.get("_restaurante_activo_ts")
    if not rid or not ts_raw:
        return None
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None
    if datetime.utcnow() - ts >= timedelta(hours=2):
        return None
    return next((a for a in todos_accesos if a.get("id") == rid), None)


MENSAJE_NO_REGISTRADO = (
    "¡Hola! 👋 Soy el asistente de Digitaly Pro. Este servicio está disponible "
    "exclusivamente para nuestros clientes suscritos. Si tienes un restaurante y "
    "te gustaría consultar tus ventas, inventario y más directamente desde WhatsApp, "
    "escríbenos a contacto@digitalypro.com o visita digitalypro.com para más información."
)

MENSAJE_ELEGIR_RESTAURANTE = (
    "Tienes acceso a varios restaurantes: {nombres}. ¿Cuál quieres consultar?"
)


def is_usuario_registrado(phone_number: str) -> bool:
    """Verifica si el número existe en la tabla usuarios, sin crear nada."""
    sb = get_supabase()
    if not sb:
        return False
    try:
        res = sb.table("usuarios") \
            .select("id") \
            .eq("whatsapp_number", phone_number) \
            .limit(1) \
            .execute()
        return bool(res.data)
    except Exception as exc:
        logging.error("Error is_usuario_registrado | %s", exc)
        return False


def get_cliente_completo(phone_number: str) -> dict | None:
    """Busca el usuario por whatsapp_number en la tabla usuarios, luego trae todos sus
    accesos activos con datos de clientes (credenciales Fudo).
    - 1 acceso activo → devuelve el dict del cliente (igual que antes).
    - N accesos activos → devuelve el primero como default + _todos_accesos + _usuario_nombre.
    - Sin accesos activos → None.
    Si el número no existe en usuarios, devuelve None (ya no se auto-crea nada;
    is_usuario_registrado() filtra estos casos antes de llegar aquí)."""
    sb = get_supabase()
    if not sb:
        return None
    try:
        res_usuario = sb.table("usuarios") \
            .select("id, nombre, restaurante_activo_id, restaurante_activo_ts") \
            .eq("whatsapp_number", phone_number) \
            .limit(1) \
            .execute()

        if not res_usuario.data:
            logging.info("Usuario no encontrado | phone=%s", phone_number)
            return None

        usuario = res_usuario.data[0]
        usuario_id = usuario["id"]
        usuario_nombre = usuario.get("nombre") or ""

        res_accesos = sb.table("accesos") \
            .select("rol, clientes(*)") \
            .eq("usuario_id", usuario_id) \
            .eq("activo", True) \
            .order("created_at") \
            .execute()

        clientes_list = [
            a["clientes"] for a in (res_accesos.data or []) if a.get("clientes")
        ]

        if not clientes_list:
            logging.info("Sin accesos activos | phone=%s", phone_number)
            return None

        cliente = dict(clientes_list[0])
        cliente["_usuario_nombre"] = usuario_nombre
        cliente["_usuario_id"] = usuario_id
        cliente["_restaurante_activo_id"] = usuario.get("restaurante_activo_id")
        cliente["_restaurante_activo_ts"] = usuario.get("restaurante_activo_ts")
        if len(clientes_list) > 1:
            cliente["_todos_accesos"] = clientes_list
        return cliente
    except Exception as exc:
        logging.error("Error get_cliente_completo | %s", exc)
    return None


def actualizar_restaurante_activo(usuario_id: str | None, cliente_id: str | None) -> None:
    """Guarda cuál restaurante eligió (por match o respuesta explícita) este usuario,
    para poder recordarlo en mensajes siguientes sin volver a preguntar."""
    sb = get_supabase()
    if not sb or not usuario_id or not cliente_id:
        return
    try:
        sb.table("usuarios").update({
            "restaurante_activo_id": cliente_id,
            "restaurante_activo_ts": datetime.utcnow().isoformat(),
        }).eq("id", usuario_id).execute()
    except Exception as exc:
        logging.error("Error actualizar_restaurante_activo | %s", exc)


def guardar_pregunta_pendiente(usuario_id: str | None, mensaje: str) -> None:
    """Guarda el mensaje original del usuario cuando el bot corta para preguntar cuál
    restaurante (ambigüedad), para poder reusarlo apenas responda (ver
    consumir_pregunta_pendiente)."""
    sb = get_supabase()
    if not sb or not usuario_id:
        return
    try:
        sb.table("usuarios").update({
            "pregunta_pendiente": mensaje,
            "pregunta_pendiente_ts": datetime.utcnow().isoformat(),
        }).eq("id", usuario_id).execute()
    except Exception as exc:
        logging.error("Error guardar_pregunta_pendiente | %s", exc)


def consumir_pregunta_pendiente(usuario_id: str | None, nombre_restaurante: str, mensaje_actual: str) -> str:
    """Si hay una pregunta_pendiente vigente (menos de 10 minutos) para este usuario,
    decide si el mensaje actual es solo la confirmación del restaurante (≤3 palabras
    tras sacarle el nombre matcheado -> usa la pregunta pendiente) o una pregunta nueva
    con contenido propio (usa el mensaje tal cual). En ambos casos limpia
    pregunta_pendiente/ts: se consume una sola vez. Si no hay pendiente vigente,
    devuelve mensaje_actual sin tocar nada."""
    sb = get_supabase()
    if not sb or not usuario_id:
        return mensaje_actual
    try:
        res = sb.table("usuarios") \
            .select("pregunta_pendiente, pregunta_pendiente_ts") \
            .eq("id", usuario_id) \
            .limit(1) \
            .execute()
        if not res.data:
            return mensaje_actual

        pendiente = res.data[0].get("pregunta_pendiente")
        ts_raw = res.data[0].get("pregunta_pendiente_ts")
        if not pendiente or not ts_raw:
            return mensaje_actual
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except (ValueError, TypeError):
            return mensaje_actual
        if datetime.utcnow() - ts >= timedelta(minutes=10):
            return mensaje_actual

        # Pendiente vigente encontrada -> se consume una sola vez, limpiar ya
        sb.table("usuarios").update({
            "pregunta_pendiente": None,
            "pregunta_pendiente_ts": None,
        }).eq("id", usuario_id).execute()

        resto = mensaje_actual.lower().replace((nombre_restaurante or "").lower(), "").strip()
        if len(resto.split()) <= 3:
            logging.info("Pregunta pendiente reusada | usuario=%s", usuario_id)
            return pendiente

        logging.info("Pregunta pendiente descartada (mensaje nuevo con contenido) | usuario=%s", usuario_id)
        return mensaje_actual
    except Exception as exc:
        logging.error("Error consumir_pregunta_pendiente | %s", exc)
        return mensaje_actual


def get_or_create_conversacion(cliente_id: str, usuario_id: str | None = None) -> str | None:
    """Obtiene la conversación activa del cliente+usuario o crea una nueva.
    usuario_id distingue conversaciones entre distintas personas con acceso al
    mismo restaurante (cliente_id solo no alcanza: ver conversaciones.usuario_id)."""
    sb = get_supabase()
    if not sb:
        return None
    try:
        desde = (datetime.utcnow() - timedelta(hours=2)).isoformat()
        query = sb.table("conversaciones") \
            .select("id") \
            .eq("cliente_id", cliente_id) \
            .is_("fecha_fin", "null") \
            .gte("fecha_inicio", desde)
        query = query.eq("usuario_id", usuario_id) if usuario_id else query.is_("usuario_id", "null")
        result = query.order("fecha_inicio", desc=True).limit(1).execute()
        if result.data:
            return result.data[0]["id"]
        payload = {
            "cliente_id": cliente_id,
            "fecha_inicio": datetime.utcnow().isoformat(),
            "pais": "Chile",
        }
        if usuario_id:
            payload["usuario_id"] = usuario_id
        nueva = sb.table("conversaciones").insert(payload).execute()
        if nueva.data:
            return nueva.data[0]["id"]
    except Exception as exc:
        logging.error("Error get_or_create_conversacion | %s", exc)
    return None


def guardar_mensaje(conversacion_id: str, rol: str, contenido: str, tokens: int = 0) -> None:
    sb = get_supabase()
    if not sb or not conversacion_id:
        return
    try:
        sb.table("mensajes").insert({
            "conversacion_id": conversacion_id,
            "rol": rol,
            "contenido": contenido if isinstance(contenido, str) else json.dumps(contenido, ensure_ascii=False),
            "timestamp": datetime.utcnow().isoformat(),
            "tokens_usados": tokens
        }).execute()
    except Exception as exc:
        logging.error("Error guardar_mensaje | %s", exc)


def cargar_historial(cliente_id: str, usuario_id: str | None = None, limite: int = 20) -> list[dict]:
    sb = get_supabase()
    if not sb:
        return []
    try:
        query = sb.table("conversaciones") \
            .select("id") \
            .eq("cliente_id", cliente_id)
        query = query.eq("usuario_id", usuario_id) if usuario_id else query.is_("usuario_id", "null")
        convs = query.order("fecha_inicio", desc=True) \
            .limit(3) \
            .execute()
        if not convs.data:
            return []
        conv_ids = [c["id"] for c in convs.data]

        mensajes = sb.table("mensajes") \
            .select("rol, contenido, timestamp") \
            .in_("conversacion_id", conv_ids) \
            .order("timestamp", desc=False) \
            .limit(limite) \
            .execute()

        historial = []
        for m in mensajes.data:
            contenido = m["contenido"]
            try:
                contenido = json.loads(contenido)
            except Exception:
                pass
            historial.append({"role": m["rol"], "content": contenido})
        return historial
    except Exception as exc:
        logging.error("Error cargar_historial | %s", exc)
    return []


def actualizar_contexto(cliente_id: str, pregunta: str, respuesta: str) -> None:
    sb = get_supabase()
    if not sb:
        return
    try:
        existing = sb.table("contexto_cliente").select("id, preguntas_frecuentes").eq("cliente_id", cliente_id).execute()

        if existing.data:
            ctx = existing.data[0]
            preguntas = ctx.get("preguntas_frecuentes") or []
            if isinstance(preguntas, str):
                preguntas = json.loads(preguntas)

            if pregunta not in preguntas:
                preguntas = ([pregunta] + preguntas)[:20]

            sb.table("contexto_cliente").update({
                "preguntas_frecuentes": preguntas,
                "ultimo_dato": {"pregunta": pregunta, "respuesta": respuesta[:500]},
                "actualizado_en": datetime.utcnow().isoformat()
            }).eq("cliente_id", cliente_id).execute()
        else:
            sb.table("contexto_cliente").insert({
                "cliente_id": cliente_id,
                "preguntas_frecuentes": [pregunta],
                "ultimo_dato": {"pregunta": pregunta, "respuesta": respuesta[:500]},
                "pais": "Chile",
                "actualizado_en": datetime.utcnow().isoformat()
            }).execute()
    except Exception as exc:
        logging.error("Error actualizar_contexto | %s", exc)


# ──────────────────────────────────────────────────────────────
# Fudo API Client (ahora instanciable por cliente)
# ──────────────────────────────────────────────────────────────

FUDO_BASE_URL = os.environ.get("FUDO_BASE_URL", "https://api.fu.do/v1alpha1")
FUDO_AUTH_URL = "https://auth.fu.do/api"


class FudoClient:
    """Cliente de solo lectura para la API de Fudo con renovación automática de token.
    Recibe sus propias credenciales — ya no depende de variables de entorno globales."""

    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self._token: str | None = None
        self._token_expiry: datetime | None = None
        self._lock = threading.Lock()

    def _refresh_token(self) -> None:
        resp = requests.post(
            FUDO_AUTH_URL,
            json={"apiKey": self.api_key, "apiSecret": self.api_secret},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("token") or data.get("access_token") or data.get("jwt")
        if not token:
            raise ValueError(f"Fudo no devolvió token. Respuesta: {data}")
        self._token = token
        self._token_expiry = datetime.utcnow() + timedelta(minutes=50)
        logging.info("Fudo token renovado")

    def _get_token(self) -> str:
        with self._lock:
            if not self._token or datetime.utcnow() >= (self._token_expiry or datetime.min):
                self._refresh_token()
            return self._token  # type: ignore[return-value]

    def get(self, endpoint: str, raw_query: str | None = None) -> Any:
        url = f"{FUDO_BASE_URL}{endpoint}"
        if raw_query:
            url = f"{url}?{raw_query}"
        for attempt in range(2):
            token = self._get_token()
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
            if resp.status_code == 401 and attempt == 0:
                with self._lock:
                    self._token = None
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("Fudo: autenticación fallida tras 2 intentos")


# Cache de clientes Fudo ya creados, para no recrear el objeto (y perder el token) en cada mensaje
_fudo_clients_cache: dict[str, FudoClient] = {}
_fudo_cache_lock = threading.Lock()


def get_fudo_client_for(cliente_info: dict | None) -> FudoClient | None:
    """Obtiene (o crea) el FudoClient correspondiente a las credenciales de ESTE cliente."""
    if not cliente_info:
        return None

    fudo_key = cliente_info.get("fudo_key")
    fudo_secret = cliente_info.get("fudo_secret")

    # Las credenciales vienen cifradas desde Supabase (ver crypto_utils.py) -> descifrar
    # antes de usarlas. Si no descifran (p.ej. texto plano sin migrar todavía), se
    # tratan como ausentes para no romper la request; ver script de migración.
    try:
        fudo_key = decrypt_value(fudo_key)
        fudo_secret = decrypt_value(fudo_secret)
    except ValueError as exc:
        logging.error(
            "No se pudieron descifrar credenciales Fudo | cliente_id=%s | %s",
            cliente_info.get("id"), exc,
        )
        fudo_key = None
        fudo_secret = None

    # Fallback: si el cliente no tiene credenciales propias, usar las globales de Railway
    # (útil para el restaurante demo / pruebas, antes de tener credenciales reales por cliente)
    if (not fudo_key or not fudo_secret or fudo_key == "TU_FUDO_KEY_AQUI") and _FUDO_GLOBAL_ENABLED:
        fudo_key = os.environ.get("FUDO_API_KEY")
        fudo_secret = os.environ.get("FUDO_API_SECRET")

    if not fudo_key or not fudo_secret or fudo_key == "TU_FUDO_KEY_AQUI":
        return None

    cache_key = f"{cliente_info.get('id')}:{fudo_key}"
    with _fudo_cache_lock:
        if cache_key in _fudo_clients_cache:
            return _fudo_clients_cache[cache_key]
        client = FudoClient(fudo_key, fudo_secret)
        _fudo_clients_cache[cache_key] = client
        return client


def _fudo_get(endpoint: str, raw_query: str | None = None) -> dict:
    """Llama a Fudo usando el cliente del restaurante activo en este momento (contextvar)."""
    client = _current_fudo_client.get()
    if not client:
        return {"error": "Fudo no configurado para este restaurante. Faltan credenciales fudo_key / fudo_secret."}
    try:
        return client.get(endpoint, raw_query)
    except Exception as exc:
        logging.error("Fudo API error | endpoint=%s | %s", endpoint, exc)
        return {"error": str(exc)}


# ──────────────────────────────────────────────────────────────
# Funciones de consulta Fudo (solo lectura)
# ──────────────────────────────────────────────────────────────

def _date_filter(from_date: str, to_date: str) -> str:
    return f"and(gte.{from_date}T00:00:00Z,lte.{to_date}T23:59:59Z)"

def get_sales_report(from_date: str, to_date: str) -> dict:
    return _fudo_get("/sales", f"filter[createdAt]={_date_filter(from_date, to_date)}")

def get_top_products(from_date: str, to_date: str, limit: int = 10) -> dict:
    return _fudo_get("/sales", f"filter[createdAt]={_date_filter(from_date, to_date)}&page[size]=100&include=items.product&sort=-createdAt")

def get_waste_report() -> dict:
    return _fudo_get("/ingredients", "page[size]=100&sort=name&include=ingredientCategory,unit&filter[stockControl]=eq.true")

def get_deliveries_report(from_date: str, to_date: str) -> dict:
    return _fudo_get("/sales", f"filter[createdAt]={_date_filter(from_date, to_date)}&filter[saleType]=eq.DELIVERY&page[size]=100&include=items")

def get_orders(from_date: str, to_date: str, status: str = "all") -> dict:
    query = f"filter[createdAt]={_date_filter(from_date, to_date)}&page[size]=100&include=items.product,payments.paymentMethod"
    if status != "all":
        query += f"&filter[saleState]=in.({status})"
    return _fudo_get("/sales", query)

def compare_periods(period1_from: str, period1_to: str, period2_from: str, period2_to: str) -> dict:
    p1 = get_sales_report(period1_from, period1_to)
    p2 = get_sales_report(period2_from, period2_to)
    if "error" in p1 or "error" in p2:
        return {"periodo_base": p1, "periodo_comparado": p2}
    try:
        def _total(d: dict) -> float:
            return float(d.get("total") or d.get("totalRevenue") or d.get("total_revenue") or 0)
        t1, t2 = _total(p1), _total(p2)
        diff = t2 - t1
        pct = round(diff / t1 * 100, 2) if t1 else None
        return {
            "periodo_base": {"desde": period1_from, "hasta": period1_to, "datos": p1},
            "periodo_comparado": {"desde": period2_from, "hasta": period2_to, "datos": p2},
            "comparacion": {"diferencia": round(diff, 2), "variacion_porcentual": pct if pct is not None else "N/A"},
        }
    except Exception:
        return {"periodo_base": p1, "periodo_comparado": p2}

def get_categories_sales() -> dict:
    return _fudo_get("/product-categories", "page[size]=100&sort=name&include=products")

def get_products(name: str | None = None, active: bool = True, stock_control: bool | None = None) -> dict:
    query = "filter[active]=eq.true&page[size]=100&sort=name&include=productCategory"
    if name:
        query += f"&filter[name]=eq.{name}"
    if stock_control:
        query += "&filter[stockControl]=eq.true"
    return _fudo_get("/products", query)

def get_ingredients(name: str | None = None, stock_control: bool | None = None) -> dict:
    query = "page[size]=100&sort=name&include=ingredientCategory,unit&fields[ingredient]=cost,minStock,name,shrinkage,stock,stockControl"
    if stock_control:
        query += "&filter[stockControl]=eq.true"
    if name:
        query += f"&filter[name]=eq.{name}"
    return _fudo_get("/ingredients", query)

def get_stock_status() -> dict:
    return _fudo_get("/ingredients", "filter[stockControl]=eq.true&page[size]=100&sort=name&fields[ingredient]=cost,minStock,name,shrinkage,stock,stockControl&include=unit")

def get_last_stock_count() -> dict:
    return _fudo_get("/products", "filter[stockControl]=eq.true&page[size]=100&sort=name&fields[product]=name,stock,minStock,lastStockCountAt,stockControl")

def get_expenses(from_date: str, to_date: str, category_id: str | None = None) -> dict:
    query = (f"filter[createdAt]=and(gte.{from_date}T00:00:00Z,lte.{to_date}T23:59:59Z)"
             "&page[size]=100&sort=-date&include=expenseCategory,provider,payments.paymentMethod")
    if category_id:
        query += f"&filter[expenseCategoryId]=eq.{category_id}"
    return _fudo_get("/expenses", query)

def get_expense_categories() -> dict:
    return _fudo_get("/expense-categories", "page[size]=100&sort=name")

def get_payments(from_date: str, to_date: str, canceled: bool = False) -> dict:
    query = (f"filter[createdAt]=and(gte.{from_date}T00:00:00Z,lte.{to_date}T23:59:59Z)"
             "&filter[canceled]=eq.false&page[size]=100&sort=-id&include=paymentMethod")
    return _fudo_get("/payments", query)

def get_payment_methods() -> dict:
    return _fudo_get("/payment-methods", "page[size]=50")

def get_customers(name: str | None = None, active: bool = True) -> dict:
    query = "filter[active]=eq.true&page[size]=100&sort=name"
    if name:
        query += f"&filter[@all]={name}"
    return _fudo_get("/customers", query)

def get_tables(include_active_sales: bool = False) -> dict:
    query = "page[size]=100&sort=number"
    if include_active_sales:
        query += "&include=activeSales,room"
    return _fudo_get("/tables", query)

def get_product_categories() -> dict:
    return _fudo_get("/product-categories", "page[size]=100&sort=name&include=products")


_TOOL_FUNCTIONS: dict[str, Any] = {
    "get_sales_report": get_sales_report,
    "get_top_products": get_top_products,
    "get_waste_report": get_waste_report,
    "get_deliveries_report": get_deliveries_report,
    "get_orders": get_orders,
    "compare_periods": compare_periods,
    "get_categories_sales": get_categories_sales,
    "get_products": get_products,
    "get_ingredients": get_ingredients,
    "get_stock_status": get_stock_status,
    "get_last_stock_count": get_last_stock_count,
    "get_expenses": get_expenses,
    "get_expense_categories": get_expense_categories,
    "get_payments": get_payments,
    "get_payment_methods": get_payment_methods,
    "get_customers": get_customers,
    "get_tables": get_tables,
    "get_product_categories": get_product_categories,
}

# ──────────────────────────────────────────────────────────────
# Herramientas Claude
# ──────────────────────────────────────────────────────────────

FUDO_TOOLS = [
    {
        "name": "get_sales_report",
        "description": "Obtiene el reporte de ventas de Fudo para un rango de fechas. Incluye total recaudado, cantidad de tickets/órdenes y ticket promedio.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "Fecha de inicio YYYY-MM-DD"},
                "to_date": {"type": "string", "description": "Fecha de fin YYYY-MM-DD"},
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "get_top_products",
        "description": "Obtiene los productos más vendidos en un rango de fechas con cantidad y monto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "Fecha de inicio YYYY-MM-DD"},
                "to_date": {"type": "string", "description": "Fecha de fin YYYY-MM-DD"},
                "limit": {"type": "integer", "description": "Cantidad de productos a retornar (default 10)"},
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "get_waste_report",
        "description": "Obtiene los ingredientes/insumos con control de stock para ver niveles de inventario y merma.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_deliveries_report",
        "description": "Obtiene datos de pedidos delivery: cantidad, montos, plataformas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "Fecha de inicio YYYY-MM-DD"},
                "to_date": {"type": "string", "description": "Fecha de fin YYYY-MM-DD"},
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "get_orders",
        "description": "Obtiene los pedidos/tickets individuales con detalle de un rango de fechas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "Fecha de inicio YYYY-MM-DD"},
                "to_date": {"type": "string", "description": "Fecha de fin YYYY-MM-DD"},
                "status": {"type": "string", "description": "Estado: 'completed', 'cancelled', 'all' (default 'all')"},
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "compare_periods",
        "description": "Compara ventas entre dos períodos. Útil para: esta semana vs la anterior, este mes vs el anterior.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period1_from": {"type": "string", "description": "Inicio del período base YYYY-MM-DD"},
                "period1_to": {"type": "string", "description": "Fin del período base YYYY-MM-DD"},
                "period2_from": {"type": "string", "description": "Inicio del período a comparar YYYY-MM-DD"},
                "period2_to": {"type": "string", "description": "Fin del período a comparar YYYY-MM-DD"},
            },
            "required": ["period1_from", "period1_to", "period2_from", "period2_to"],
        },
    },
    {
        "name": "get_categories_sales",
        "description": "Obtiene las categorías del menú con los productos que contiene cada una.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_products",
        "description": "Obtiene el listado de productos activos del menú con sus categorías.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nombre o parte del nombre del producto a buscar"},
                "stock_control": {"type": "boolean", "description": "Si true, solo devuelve productos con control de stock"},
            },
            "required": [],
        },
    },
    {
        "name": "get_ingredients",
        "description": "Obtiene ingredientes/insumos con su stock actual, unidad y categoría.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nombre o parte del nombre del ingrediente"},
                "stock_control": {"type": "boolean", "description": "Si true, solo devuelve ingredientes con control de stock"},
            },
            "required": [],
        },
    },
    {
        "name": "get_stock_status",
        "description": "Obtiene el estado actual del stock de todos los ingredientes con control de inventario.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_last_stock_count",
        "description": "Obtiene los productos con control de stock y la fecha de su último conteo de inventario.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_expenses",
        "description": "Obtiene los gastos/egresos registrados en un rango de fechas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "Fecha de inicio YYYY-MM-DD"},
                "to_date": {"type": "string", "description": "Fecha de fin YYYY-MM-DD"},
                "category_id": {"type": "string", "description": "ID de categoría de gasto para filtrar (opcional)"},
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "get_expense_categories",
        "description": "Obtiene todas las categorías de gastos/egresos disponibles en Fudo.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_payments",
        "description": "Obtiene los pagos recibidos en un rango de fechas con el método de pago de cada uno.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "Fecha de inicio YYYY-MM-DD"},
                "to_date": {"type": "string", "description": "Fecha de fin YYYY-MM-DD"},
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "get_payment_methods",
        "description": "Obtiene todos los métodos de pago configurados en Fudo.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_customers",
        "description": "Obtiene el listado de clientes activos.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nombre, teléfono o email del cliente a buscar"},
            },
            "required": [],
        },
    },
    {
        "name": "get_tables",
        "description": "Obtiene el estado de las mesas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "include_active_sales": {"type": "boolean", "description": "Si true, incluye las ventas activas en cada mesa"},
            },
            "required": [],
        },
    },
    {
        "name": "get_product_categories",
        "description": "Obtiene todas las categorías de productos del menú con los productos que contiene cada una.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

# ──────────────────────────────────────────────────────────────
# System prompt
# ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Eres el asistente de negocio de {nombre_pila} en WhatsApp, proporcionado por Digitaly Pro.
Tu rol es ser como un socio que ayuda a {nombre_pila} a entender cómo va su negocio "{nombre_restaurante}" consultando datos reales de Fudo en tiempo real.
La fecha de hoy es {today}.
{multi_restaurante_ctx}

PERSONALIDAD:
- Cercano y profesional, nunca robótico. Habla como un colega de confianza, no como un manual.
- Usa el nombre "{nombre_pila}" de forma natural al saludar o responder, pero no lo repitas en cada mensaje. Si el nombre es genérico como "dueño" o "cliente", omítelo.
- Sé directo: responde primero con el dato que pidieron, después agrega contexto si es útil.
- Si no tienes la información o Fudo no responde, dilo con honestidad: "No pude obtener esos datos en este momento, ¿quieres que lo intente de nuevo?"

FORMATO WHATSAPP (importante):
- Mensajes cortos y fáciles de leer en pantalla de celular. Máximo 3-4 párrafos cortos.
- NO uses markdown: nada de ##, **, ```, ni listas con guiones (-). WhatsApp no lo renderiza bien.
- Para listas usa números (1. 2. 3.) o saltos de línea simples.
- Usa *negrita* solo para destacar cifras importantes (WhatsApp sí soporta esto con asteriscos simples).
- Emojis con moderación: máximo 2-3 por mensaje, solo cuando aporten.
- Montos en formato local: $12.500 para Chile, $12.500 para Argentina, $12,500 para México.

TONO POR PAÍS:
- Chile: usa "tú", modismos suaves como "dale", "listo", "de una". Moneda: CLP ($).
- Argentina: usa "vos", modismos como "dale", "bárbaro", "genial". Moneda: ARS ($).
- México: usa "tú", modismos como "sale", "órale", "perfecto". Moneda: MXN ($).
- País del cliente: {pais}. Si no lo reconoces como Chile, Argentina o México, usa español neutro con "tú".

HERRAMIENTAS FUDO:
Tienes acceso a 18 funciones de solo lectura de Fudo. Usa la herramienta correcta según lo que pregunte el usuario. Las principales categorías son:
- Ventas y pedidos: reportes de ventas, productos más vendidos, deliveries, comparación de períodos, ventas por categoría
- Productos e inventario: lista de productos, ingredientes, stock, mermas, último inventario, categorías del menú
- Gastos: reportes de gastos por fecha, categorías de gastos
- Pagos: cobros por método de pago, métodos disponibles
- Clientes: búsqueda y listado de clientes
- Mesas: estado de mesas, ocupación actual
Nunca inventes datos. Si una herramienta falla o devuelve error, no intentes adivinar los números.

CUANDO FUDO NO RESPONDE:
Si una llamada a Fudo falla o devuelve error, responde algo como: "No pude conectarme a Fudo en este momento para obtener esos datos. Puede ser un tema temporal. ¿Quieres que lo intente de nuevo?" No te disculpes excesivamente ni des explicaciones técnicas.

PREGUNTAS NO RELACIONADAS AL NEGOCIO:
Si te preguntan algo que no tiene que ver con el restaurante, responde brevemente y de forma amable, pero recuerda que tu función principal es ayudar con el negocio.
"""

# ──────────────────────────────────────────────────────────────
# Claude con tool_use, memoria Supabase y Fudo multi-cliente
# ──────────────────────────────────────────────────────────────

_anthropic = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
conversation_histories: dict[str, list[dict]] = {}  # fallback RAM
_MAX_HISTORY = 40


def _execute_tool(name: str, tool_input: dict) -> Any:
    func = _TOOL_FUNCTIONS.get(name)
    if not func:
        return {"error": f"Herramienta desconocida: {name}"}
    try:
        return func(**tool_input)
    except Exception as exc:
        logging.error("Tool error | %s | %s", name, exc)
        return {"error": str(exc)}


def ask_claude(user_message: str, phone_number: str) -> str:
    # ── Identificar usuario y seleccionar restaurante activo ──
    cliente_info = get_cliente_completo(phone_number)

    todos_accesos = (cliente_info or {}).get("_todos_accesos", [])
    usuario_nombre_raw = (cliente_info or {}).get("_usuario_nombre", "")
    usuario_id = (cliente_info or {}).get("_usuario_id")
    nombre_pila = usuario_nombre_raw.split()[0].capitalize() if usuario_nombre_raw.strip() else "dueño"

    # Seleccionar restaurante para esta consulta (conservador: nombre completo literal en el mensaje)
    cliente_activo = cliente_info
    if todos_accesos:
        match = _match_restaurante(user_message, todos_accesos)
        if match:
            cliente_activo = match
            actualizar_restaurante_activo(usuario_id, match.get("id"))
            user_message = consumir_pregunta_pendiente(usuario_id, match.get("nombre_restaurante", ""), user_message)
            logging.info("Restaurante seleccionado por match | nombre=%s", match.get("nombre_restaurante"))
        else:
            vigente = _restaurante_activo_vigente(cliente_info, todos_accesos)
            if vigente:
                cliente_activo = vigente
                logging.info("Restaurante activo recordado | nombre=%s", vigente.get("nombre_restaurante"))
            else:
                nombres = [a.get("nombre_restaurante", "") for a in todos_accesos]
                nombres_txt = " o ".join(f'"{n}"' for n in nombres)
                logging.info("Restaurante ambiguo sin activo vigente | usuario=%s", usuario_id)
                guardar_pregunta_pendiente(usuario_id, user_message)
                return MENSAJE_ELEGIR_RESTAURANTE.format(nombres=nombres_txt)

    # cliente_id y conversacion_id se derivan de cliente_activo (post-match)
    cliente_id = cliente_activo.get("id") if cliente_activo else None
    conversacion_id = get_or_create_conversacion(cliente_id, usuario_id) if cliente_id else None

    fudo_client = get_fudo_client_for(cliente_activo)
    token_fudo = _current_fudo_client.set(fudo_client)
    token_info = _current_cliente_info.set(cliente_activo)

    try:
        if cliente_id:
            history = cargar_historial(cliente_id, usuario_id)
            logging.info("Historial cargado | cliente=%s | mensajes=%d", cliente_id, len(history))
        else:
            history = conversation_histories.setdefault(phone_number, [])

        guardar_mensaje(conversacion_id, "user", user_message)
        history.append({"role": "user", "content": user_message})

        nombre_restaurante = (cliente_activo or {}).get("nombre_restaurante", "tu negocio")

        if todos_accesos:
            nombres = [a.get("nombre_restaurante", "") for a in todos_accesos]
            otros = [n for n in nombres if n != nombre_restaurante]
            nombres_pregunta = " o ".join(f'"{n}"' for n in nombres)
            multi_ctx = (
                f'ACCESO MULTI-RESTAURANTE: Este usuario tiene acceso a: {", ".join(nombres)}. '
                f'Estás respondiendo con datos de "{nombre_restaurante}". '
                f'Si la pregunta no especifica a cuál restaurante se refiere, usa "{nombre_restaurante}" como predeterminado '
                f'e informa que también puede consultar {", ".join(otros)}. '
                f'Si hay ambigüedad, pregunta explícitamente "¿te refieres a {nombres_pregunta}?" antes de consultar datos.\n'
            )
        else:
            multi_ctx = ""

        pais = (cliente_activo or {}).get("pais") or "chile"

        system = SYSTEM_PROMPT.format(
            today=datetime.now().strftime("%Y-%m-%d"),
            nombre_restaurante=nombre_restaurante,
            nombre_pila=nombre_pila,
            multi_restaurante_ctx=multi_ctx,
            pais=pais,
        )
        active_tools = FUDO_TOOLS if fudo_client else []
        final_response = ""

        for _ in range(10):
            kwargs: dict = dict(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=system,
                messages=history,
            )
            if active_tools:
                kwargs["tools"] = active_tools

            response = _anthropic.messages.create(**kwargs)

            if response.stop_reason == "tool_use":
                history.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logging.info("Tool call | cliente=%s | %s | input=%s", cliente_id, block.name, block.input)
                        result = _execute_tool(block.name, block.input)
                        logging.info("Tool result | %s | %s", block.name, str(result)[:300])
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        })
                history.append({"role": "user", "content": tool_results})
                continue

            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            history.append({"role": "assistant", "content": response.content})
            final_response = text

            guardar_mensaje(conversacion_id, "assistant", text, tokens=response.usage.output_tokens if response.usage else 0)

            if cliente_id:
                actualizar_contexto(cliente_id, user_message, text)

            if not cliente_id and len(history) > _MAX_HISTORY:
                conversation_histories[phone_number] = history[-_MAX_HISTORY:]

            return final_response

        return "Lo siento, no pude procesar tu consulta en este momento."
    finally:
        _current_fudo_client.reset(token_fudo)
        _current_cliente_info.reset(token_info)


# ──────────────────────────────────────────────────────────────
# WhatsApp
# ──────────────────────────────────────────────────────────────

def send_whatsapp_message(to: str, body: str) -> None:
    token = os.environ.get("WHATSAPP_TOKEN")
    phone_id = os.environ.get("WHATSAPP_PHONE_ID")
    api_url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    logging.info("Enviando mensaje a %s", to)
    try:
        resp = requests.post(api_url, json=payload, headers=headers, timeout=10)
        logging.info("Meta API | status=%s | body=%s", resp.status_code, resp.text)
    except requests.exceptions.RequestException as exc:
        logging.error("Error Meta API: %s", exc)


# ──────────────────────────────────────────────────────────────
# Flask routes
# ──────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN")
    logging.info("Webhook verify | mode=%r | token=%r", mode, token)
    if mode == "subscribe" and token == verify_token:
        return Response(challenge, status=200, mimetype="text/plain")
    return Response("Verificación fallida", status=403, mimetype="text/plain")


@app.route("/webhook", methods=["POST"])
def webhook_receive():
    data = request.get_json(silent=True)
    if not data or data.get("object") != "whatsapp_business_account":
        return jsonify({"status": "ignored"}), 200

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                if msg.get("type") != "text":
                    continue
                sender = msg["from"]
                text = msg["text"]["body"]
                logging.info("Mensaje de %s: %s", sender, text)

                if not is_usuario_registrado(sender):
                    logging.info("Número no registrado, respuesta fija sin pasar por Claude | phone=%s", sender)
                    send_whatsapp_message(sender, MENSAJE_NO_REGISTRADO)
                    continue

                reply = ask_claude(text, sender)
                logging.info("Respuesta para %s: %s", sender, reply[:100])
                send_whatsapp_message(sender, reply)

    return jsonify({"status": "ok"}), 200


@app.route("/health", methods=["GET"])
def health():
    sb_status = "conectado" if get_supabase() else "no configurado"
    return jsonify({
        "status": "ok",
        "fudo_global_fallback": "habilitado" if _FUDO_GLOBAL_ENABLED else "no configurado",
        "supabase": sb_status
    })


@app.route("/message", methods=["POST"])
def message():
    data = request.get_json(silent=True)
    if not data or "message" not in data:
        return jsonify({"error": "El campo 'message' es requerido"}), 400
    user_message = str(data["message"]).strip()
    if not user_message:
        return jsonify({"error": "El mensaje no puede estar vacío"}), 400
    reply = ask_claude(user_message, "local")
    return jsonify({"reply": reply})


@app.route("/clientes", methods=["POST"])
def crear_o_actualizar_cliente():
    """Endpoint para dar de alta / actualizar un restaurante con sus credenciales Fudo.
    Body esperado: { nombre_restaurante, pais, whatsapp_number_user, fudo_key, fudo_secret }"""
    admin_key = os.environ.get("ADMIN_API_KEY")
    if not admin_key or request.headers.get("X-Admin-Key") != admin_key:
        return jsonify({"error": "No autorizado"}), 401

    sb = get_supabase()
    if not sb:
        return jsonify({"error": "Supabase no configurado"}), 500

    data = request.get_json(silent=True) or {}
    whatsapp_number = data.get("whatsapp_number_user")
    if not whatsapp_number:
        return jsonify({"error": "whatsapp_number_user es requerido"}), 400

    payload = {
        "nombre_restaurante": data.get("nombre_restaurante", f"Cliente {whatsapp_number}"),
        "pais": data.get("pais", "Chile"),
        "whatsapp_phone_id": data.get("whatsapp_phone_id", os.environ.get("WHATSAPP_PHONE_ID", "")),
        "whatsapp_number_user": whatsapp_number,
        "fudo_key": encrypt_value(data.get("fudo_key")),
        "fudo_secret": encrypt_value(data.get("fudo_secret")),
        "activo": True,
    }

    try:
        existing = sb.table("clientes").select("id").eq("whatsapp_number_user", whatsapp_number).execute()
        if existing.data:
            cliente_id = existing.data[0]["id"]
            sb.table("clientes").update(payload).eq("id", cliente_id).execute()
            return jsonify({"status": "actualizado", "cliente_id": cliente_id})
        else:
            nuevo = sb.table("clientes").insert(payload).execute()
            return jsonify({"status": "creado", "cliente_id": nuevo.data[0]["id"] if nuevo.data else None})
    except Exception as exc:
        logging.error("Error crear_o_actualizar_cliente | %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/reset", methods=["POST"])
def reset():
    conversation_histories.clear()
    return jsonify({"status": "Historial de conversación borrado"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)

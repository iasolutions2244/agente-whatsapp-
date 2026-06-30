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


def get_cliente_completo(phone_number: str) -> dict | None:
    """Busca el usuario por whatsapp_number en la tabla usuarios, luego trae todos sus
    accesos activos con datos de clientes (credenciales Fudo).
    - 1 acceso activo → devuelve el dict del cliente (igual que antes).
    - N accesos activos → devuelve el primero como default + _todos_accesos + _usuario_nombre.
    - Sin accesos activos → None.
    Si el número no existe en usuarios, crea un cliente demo (comportamiento legado).
    clientes_usuarios se mantiene sin tocar como respaldo."""
    sb = get_supabase()
    if not sb:
        return None
    try:
        res_usuario = sb.table("usuarios") \
            .select("id, nombre") \
            .eq("whatsapp_number", phone_number) \
            .limit(1) \
            .execute()

        if res_usuario.data:
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
            if len(clientes_list) > 1:
                cliente["_todos_accesos"] = clientes_list
            return cliente

        # Fallback: crear cliente demo para números no registrados
        nuevo = sb.table("clientes").insert({
            "nombre_restaurante": f"Cliente {phone_number}",
            "pais": "Chile",
            "whatsapp_phone_id": os.environ.get("WHATSAPP_PHONE_ID", ""),
            "whatsapp_number_user": phone_number,
            "activo": True
        }).execute()
        if nuevo.data:
            cliente = nuevo.data[0]
            cliente_id = cliente["id"]
            logging.info("Nuevo cliente demo creado | id=%s | phone=%s", cliente_id, phone_number)
            try:
                res_u = sb.table("usuarios").insert({
                    "whatsapp_number": phone_number,
                    "nombre": f"Cliente {phone_number}",
                }).execute()
                if res_u.data:
                    sb.table("accesos").insert({
                        "usuario_id": res_u.data[0]["id"],
                        "cliente_id": cliente_id,
                        "rol": "operador",
                        "activo": True,
                    }).execute()
                sb.table("clientes_usuarios").insert({
                    "cliente_id": cliente_id,
                    "whatsapp_number": phone_number,
                    "nombre": f"Cliente {phone_number}",
                    "rol": "operador",
                    "activo": True,
                }).execute()
            except Exception:
                pass
            return cliente
    except Exception as exc:
        logging.error("Error get_cliente_completo | %s", exc)
    return None


def get_or_create_conversacion(cliente_id: str) -> str | None:
    """Obtiene la conversación activa del cliente o crea una nueva."""
    sb = get_supabase()
    if not sb:
        return None
    try:
        desde = (datetime.utcnow() - timedelta(hours=2)).isoformat()
        result = sb.table("conversaciones") \
            .select("id") \
            .eq("cliente_id", cliente_id) \
            .is_("fecha_fin", "null") \
            .gte("fecha_inicio", desde) \
            .order("fecha_inicio", desc=True) \
            .limit(1) \
            .execute()
        if result.data:
            return result.data[0]["id"]
        nueva = sb.table("conversaciones").insert({
            "cliente_id": cliente_id,
            "fecha_inicio": datetime.utcnow().isoformat(),
            "pais": "Chile"
        }).execute()
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


def cargar_historial(cliente_id: str, limite: int = 20) -> list[dict]:
    sb = get_supabase()
    if not sb:
        return []
    try:
        convs = sb.table("conversaciones") \
            .select("id") \
            .eq("cliente_id", cliente_id) \
            .order("fecha_inicio", desc=True) \
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
    return _fudo_get("/sales", f"filter[createdAt]={_date_filter(from_date, to_date)}&filter[saleType]=DELIVERY&page[size]=100&include=items")

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
        query += f"&filter[name]={name}"
    if stock_control:
        query += "&filter[stockControl]=eq.true"
    return _fudo_get("/products", query)

def get_ingredients(name: str | None = None, stock_control: bool | None = None) -> dict:
    query = "page[size]=100&sort=name&include=ingredientCategory,unit&fields[ingredient]=cost,minStock,name,shrinkage,stock,stockControl"
    if stock_control:
        query += "&filter[stockControl]=eq.true"
    if name:
        query += f"&filter[name]={name}"
    return _fudo_get("/ingredients", query)

def get_stock_status() -> dict:
    return _fudo_get("/ingredients", "filter[stockControl]=eq.true&page[size]=100&sort=name&fields[ingredient]=cost,minStock,name,shrinkage,stock,stockControl&include=unit")

def get_last_stock_count() -> dict:
    return _fudo_get("/products", "filter[stockControl]=eq.true&page[size]=100&sort=name&fields[product]=name,stock,minStock,lastStockCountAt,stockControl")

def get_expenses(from_date: str, to_date: str, category_id: str | None = None) -> dict:
    query = (f"filter[createdAt]=and(gte.{from_date}T00:00:00Z,lte.{to_date}T23:59:59Z)"
             "&page[size]=100&sort=-date&include=expenseCategory,provider,payments.paymentMethod")
    if category_id:
        query += f"&filter[expenseCategoryId]={category_id}"
    return _fudo_get("/expenses", query)

def get_expense_categories() -> dict:
    return _fudo_get("/expense-categories", "page[size]=100&sort=name")

def get_payments(from_date: str, to_date: str, canceled: bool = False) -> dict:
    query = (f"filter[createdAt]=and(gte.{from_date}T00:00:00Z,lte.{to_date}T23:59:59Z)"
             "&filter[canceled]=false&page[size]=100&sort=-id&include=paymentMethod")
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

SYSTEM_PROMPT = """Eres el asistente de negocio de {nombre_pila}, disponible por WhatsApp.
Dirígete a la persona por su nombre de pila ("{nombre_pila}") de forma natural cuando sea apropiado — al saludar, al responder una pregunta puntual, o en cualquier momento de la conversación. Si el nombre es genérico como "dueño", omítelo.
Tienes acceso en tiempo real a los datos de Fudo (sistema de gestión del local de {nombre_restaurante}).
La fecha de hoy es {today}.
{multi_restaurante_ctx}
Cuando el dueño pregunta sobre su negocio, usas las herramientas de Fudo para consultar datos reales y responder con información precisa.
Nunca inventas datos: si no tienes la información o la API falla, lo dices claramente.

VENTAS Y PEDIDOS:
- "¿Cuánto vendimos hoy / esta semana / este mes?" → get_sales_report
- "¿Cuáles son los productos más vendidos?" → get_top_products
- "¿Cómo estuvo el delivery?" → get_deliveries_report
- "¿Cuánto hay de merma?" → get_waste_report
- "Comparame las ventas de esta semana con la anterior" → compare_periods
- "Ventas por categoría" → get_categories_sales

PRODUCTOS E INVENTARIO:
- "¿Qué productos tenemos?" / "¿Está activo el producto X?" → get_products
- "¿Cuánto stock tiene el ingrediente X?" → get_ingredients
- "¿Cuánto stock queda de X?" → get_stock_status o get_ingredients con name
- "¿Qué ingredientes tienen merma?" → get_stock_status, mostrar los que tienen shrinkage > 0
- "¿Hay ingredientes sin stock?" → get_stock_status, mostrar los que tienen stock = null o stock = 0
- "¿Cuándo fue el último inventario?" → get_last_stock_count
- "¿Cuáles son las categorías del menú?" → get_product_categories

GASTOS Y EGRESOS:
- "¿Cuáles son los gastos de hoy / esta semana?" → get_expenses con from_date y to_date
- "¿Cuáles son las categorías de gastos?" → get_expense_categories

PAGOS:
- "¿Cuánto se cobró hoy en efectivo / tarjeta?" → get_payments
- "¿Cuáles son los métodos de pago?" → get_payment_methods

CLIENTES:
- "¿Cuántos clientes tenemos?" / "Busca el cliente X" → get_customers

MESAS:
- "¿Qué mesas están ocupadas ahora?" → get_tables con include_active_sales=true
- "¿Cuántas mesas hay?" → get_tables

Responde de forma clara y directa. Cuando muestres montos usa el formato local (ej: $12.500).
Para preguntas que no son del negocio, responde normalmente."""

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
    nombre_pila = usuario_nombre_raw.split()[0].capitalize() if usuario_nombre_raw.strip() else "dueño"

    # Seleccionar restaurante para esta consulta (conservador: nombre completo literal en el mensaje)
    cliente_activo = cliente_info
    if todos_accesos:
        match = _match_restaurante(user_message, todos_accesos)
        if match:
            cliente_activo = match
            logging.info("Restaurante seleccionado por match | nombre=%s", match.get("nombre_restaurante"))

    # cliente_id y conversacion_id se derivan de cliente_activo (post-match)
    cliente_id = cliente_activo.get("id") if cliente_activo else None
    conversacion_id = get_or_create_conversacion(cliente_id) if cliente_id else None

    fudo_client = get_fudo_client_for(cliente_activo)
    token_fudo = _current_fudo_client.set(fudo_client)
    token_info = _current_cliente_info.set(cliente_activo)

    try:
        if cliente_id:
            history = cargar_historial(cliente_id)
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

        system = SYSTEM_PROMPT.format(
            today=datetime.now().strftime("%Y-%m-%d"),
            nombre_restaurante=nombre_restaurante,
            nombre_pila=nombre_pila,
            multi_restaurante_ctx=multi_ctx,
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
        "fudo_key": data.get("fudo_key"),
        "fudo_secret": data.get("fudo_secret"),
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

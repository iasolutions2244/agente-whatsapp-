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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

load_dotenv()

for _var in ["ANTHROPIC_API_KEY", "WHATSAPP_TOKEN", "WHATSAPP_PHONE_ID", "WHATSAPP_VERIFY_TOKEN"]:
    logging.info("ENV CHECK | %s = %s", _var, "SET" if os.environ.get(_var) else "*** MISSING ***")

_FUDO_ENABLED = bool(os.environ.get("FUDO_API_KEY") and os.environ.get("FUDO_API_SECRET"))
logging.info("FUDO | habilitado=%s", _FUDO_ENABLED)

app = Flask(__name__)

# ──────────────────────────────────────────────────────────────
# Fudo API Client
# ──────────────────────────────────────────────────────────────

FUDO_BASE_URL = os.environ.get("FUDO_BASE_URL", "https://app.fudo.com.ar/api/v1")


class FudoClient:
    """Cliente de solo lectura para la API de Fudo con renovación automática de token."""

    def __init__(self) -> None:
        self.api_key = os.environ.get("FUDO_API_KEY", "")
        self.api_secret = os.environ.get("FUDO_API_SECRET", "")
        self._token: str | None = None
        self._token_expiry: datetime | None = None
        self._lock = threading.Lock()

    def _refresh_token(self) -> None:
        resp = requests.post(
            f"{FUDO_BASE_URL}/auth/token",
            json={"api_key": self.api_key, "api_secret": self.api_secret},
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

    def get(self, endpoint: str, params: dict | None = None) -> Any:
        """GET autenticado con reintento automático ante token expirado."""
        for attempt in range(2):
            token = self._get_token()
            resp = requests.get(
                f"{FUDO_BASE_URL}{endpoint}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=20,
            )
            if resp.status_code == 401 and attempt == 0:
                with self._lock:
                    self._token = None
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("Fudo: autenticación fallida tras 2 intentos")


_fudo_client = FudoClient()


def _fudo_get(endpoint: str, params: dict | None = None) -> dict:
    """Llama a Fudo y retorna un dict de error en vez de lanzar excepción."""
    if not _FUDO_ENABLED:
        return {"error": "Fudo no configurado. Agrega FUDO_API_KEY y FUDO_API_SECRET al .env"}
    try:
        return _fudo_client.get(endpoint, params)
    except Exception as exc:
        logging.error("Fudo API error | endpoint=%s | %s", endpoint, exc)
        return {"error": str(exc)}


# ──────────────────────────────────────────────────────────────
# Funciones de consulta (solo lectura)
# ──────────────────────────────────────────────────────────────

def get_sales_report(from_date: str, to_date: str) -> dict:
    return _fudo_get("/reports/sales", {"from": from_date, "to": to_date})


def get_top_products(from_date: str, to_date: str, limit: int = 10) -> dict:
    return _fudo_get("/reports/products", {"from": from_date, "to": to_date, "limit": limit})


def get_waste_report(from_date: str, to_date: str) -> dict:
    return _fudo_get("/reports/waste", {"from": from_date, "to": to_date})


def get_deliveries_report(from_date: str, to_date: str) -> dict:
    return _fudo_get("/reports/deliveries", {"from": from_date, "to": to_date})


def get_orders(from_date: str, to_date: str, status: str = "all") -> dict:
    params: dict = {"from": from_date, "to": to_date}
    if status != "all":
        params["status"] = status
    return _fudo_get("/orders", params)


def compare_periods(
    period1_from: str, period1_to: str, period2_from: str, period2_to: str
) -> dict:
    p1 = get_sales_report(period1_from, period1_to)
    p2 = get_sales_report(period2_from, period2_to)
    if "error" in p1 or "error" in p2:
        return {"periodo_base": p1, "periodo_comparado": p2}
    try:
        def _total(d: dict) -> float:
            return float(
                d.get("total") or d.get("totalRevenue") or d.get("total_revenue") or 0
            )
        t1, t2 = _total(p1), _total(p2)
        diff = t2 - t1
        pct = round(diff / t1 * 100, 2) if t1 else None
        return {
            "periodo_base": {"desde": period1_from, "hasta": period1_to, "datos": p1},
            "periodo_comparado": {"desde": period2_from, "hasta": period2_to, "datos": p2},
            "comparacion": {
                "diferencia": round(diff, 2),
                "variacion_porcentual": pct if pct is not None else "N/A",
            },
        }
    except Exception:
        return {"periodo_base": p1, "periodo_comparado": p2}


def get_categories_sales(from_date: str, to_date: str) -> dict:
    return _fudo_get("/reports/categories", {"from": from_date, "to": to_date})


_TOOL_FUNCTIONS: dict[str, Any] = {
    "get_sales_report": get_sales_report,
    "get_top_products": get_top_products,
    "get_waste_report": get_waste_report,
    "get_deliveries_report": get_deliveries_report,
    "get_orders": get_orders,
    "compare_periods": compare_periods,
    "get_categories_sales": get_categories_sales,
}

# ──────────────────────────────────────────────────────────────
# Definición de herramientas para Claude
# ──────────────────────────────────────────────────────────────

FUDO_TOOLS = [
    {
        "name": "get_sales_report",
        "description": (
            "Obtiene el reporte de ventas de Fudo para un rango de fechas. "
            "Incluye total recaudado, cantidad de tickets/órdenes y ticket promedio."
        ),
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
        "description": "Obtiene el reporte de mermas y desperdicios registrados en Fudo.",
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
                "status": {
                    "type": "string",
                    "description": "Estado: 'completed', 'cancelled', 'all' (default 'all')",
                },
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "compare_periods",
        "description": (
            "Compara ventas entre dos períodos. "
            "Útil para: esta semana vs la anterior, este mes vs el anterior, etc."
        ),
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
        "description": "Obtiene ventas desglosadas por categoría de producto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "Fecha de inicio YYYY-MM-DD"},
                "to_date": {"type": "string", "description": "Fecha de fin YYYY-MM-DD"},
            },
            "required": ["from_date", "to_date"],
        },
    },
]

# ──────────────────────────────────────────────────────────────
# System prompt
# ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Eres el asistente de negocio del dueño, disponible por WhatsApp.
Tienes acceso en tiempo real a los datos de Fudo (sistema de gestión del local).
La fecha de hoy es {today}.

Cuando el dueño pregunta sobre su negocio, usas las herramientas de Fudo para consultar datos reales y responder con información precisa.
Nunca inventas datos: si no tienes la información o la API falla, lo dices claramente.

Puedes responder preguntas como:
- ¿Cuánto vendimos hoy / esta semana / este mes?
- ¿Cuáles son los productos más vendidos?
- ¿Cómo estuvo el delivery?
- ¿Cuánto hay de merma?
- Comparame las ventas de esta semana con la anterior

Responde de forma clara y directa. Cuando muestres montos usa el formato local (ej: $12.500).
Para preguntas que no son del negocio, responde normalmente."""

# ──────────────────────────────────────────────────────────────
# Claude con tool_use
# ──────────────────────────────────────────────────────────────

_anthropic = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
conversation_histories: dict[str, list[dict]] = {}

_MAX_HISTORY = 40  # máximo de mensajes guardados por número


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
    history = conversation_histories.setdefault(phone_number, [])
    history.append({"role": "user", "content": user_message})

    system = SYSTEM_PROMPT.format(today=datetime.now().strftime("%Y-%m-%d"))
    active_tools = FUDO_TOOLS if _FUDO_ENABLED else []

    for _ in range(10):  # máximo 10 rondas de tool_use por mensaje
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
                    logging.info("Tool call | %s | input=%s", block.name, block.input)
                    result = _execute_tool(block.name, block.input)
                    logging.info("Tool result | %s | %s", block.name, str(result)[:300])
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
            history.append({"role": "user", "content": tool_results})
            continue

        # end_turn u otro stop_reason → respuesta final
        text = next((b.text for b in response.content if hasattr(b, "text")), "")
        history.append({"role": "assistant", "content": response.content})

        # Recortar historial si creció demasiado
        if len(history) > _MAX_HISTORY:
            conversation_histories[phone_number] = history[-_MAX_HISTORY:]

        return text

    return "Lo siento, no pude procesar tu consulta en este momento."


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
    return jsonify({"status": "ok", "fudo": "habilitado" if _FUDO_ENABLED else "no configurado"})


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


@app.route("/reset", methods=["POST"])
def reset():
    conversation_histories.clear()
    return jsonify({"status": "Historial de conversación borrado"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)

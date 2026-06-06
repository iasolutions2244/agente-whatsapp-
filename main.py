import os
import logging
import requests
from flask import Flask, request, jsonify
from anthropic import Anthropic
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

load_dotenv()

app = Flask(__name__)
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"

SYSTEM_PROMPT = """Eres un asistente útil conectado a WhatsApp.
Responde de forma clara y concisa.
Cuando el contexto lo requiera, puedes usar listas o emojis para mayor claridad."""

# Historial de conversación por número de teléfono
conversation_histories: dict[str, list[dict]] = {}


def ask_claude(user_message: str, phone_number: str) -> str:
    history = conversation_histories.setdefault(phone_number, [])
    history.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=history,
    )

    assistant_message = response.content[0].text
    history.append({"role": "assistant", "content": assistant_message})

    return assistant_message


def send_whatsapp_message(to: str, body: str) -> None:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    logging.info("Enviando mensaje a %s | URL: %s", to, WHATSAPP_API_URL)
    try:
        resp = requests.post(WHATSAPP_API_URL, json=payload, headers=headers, timeout=10)
        logging.info("Respuesta Meta API | status=%s | body=%s", resp.status_code, resp.text)
    except requests.exceptions.RequestException as e:
        logging.error("Error al llamar Meta API: %s", e)


@app.route("/webhook", methods=["GET"])
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        return challenge, 200

    return "Verificación fallida", 403


@app.route("/webhook", methods=["POST"])
def webhook_receive():
    data = request.get_json(silent=True)

    if not data or data.get("object") != "whatsapp_business_account":
        return jsonify({"status": "ignored"}), 200

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            for msg in messages:
                if msg.get("type") != "text":
                    continue
                sender = msg["from"]
                text = msg["text"]["body"]
                logging.info("Mensaje recibido de %s: %s", sender, text)
                reply = ask_claude(text, sender)
                logging.info("Respuesta Claude para %s: %s", sender, reply[:100])
                send_whatsapp_message(sender, reply)

    return jsonify({"status": "ok"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


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
    port = int(os.getenv("PORT", 5000))
    app.run(debug=True, port=port)

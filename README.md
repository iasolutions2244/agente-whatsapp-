# Agente WhatsApp + Claude

Servidor Flask base que recibe mensajes de texto y responde usando la API de Claude (Anthropic). Punto de partida para integrar con WhatsApp Business API.

## Requisitos

- Python 3.10+
- Una API key de Anthropic → [console.anthropic.com](https://console.anthropic.com)

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración

Edita el archivo `.env` y reemplaza el valor de `ANTHROPIC_API_KEY`:

```
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxx
PORT=5000
```

## Iniciar el servidor

```bash
python main.py
```

El servidor queda escuchando en `http://localhost:5000`.

## Endpoints

### `GET /health`
Verifica que el servidor está activo.

```bash
curl http://localhost:5000/health
```

### `POST /message`
Envía un mensaje y recibe la respuesta de Claude.

**Body JSON:**
```json
{ "message": "Hola, ¿cómo estás?" }
```

**Ejemplo con curl:**
```bash
curl -X POST http://localhost:5000/message \
  -H "Content-Type: application/json" \
  -d '{"message": "Explícame qué es Python en 2 oraciones"}'
```

**Respuesta:**
```json
{ "reply": "Python es un lenguaje de programación..." }
```

### `POST /reset`
Borra el historial de conversación en memoria.

```bash
curl -X POST http://localhost:5000/reset
```

## Notas

- El historial de conversación se guarda en memoria mientras el servidor esté corriendo. Al reiniciar se borra.
- El endpoint `/message` es el que luego se conectará al webhook de WhatsApp Business.
- El modelo usado es `claude-sonnet-4-6`. Puedes cambiarlo en `main.py`.

## Próximos pasos (integración WhatsApp)

1. Exponer el servidor públicamente con [ngrok](https://ngrok.com) o desplegarlo en un VPS.
2. Crear una app en [Meta for Developers](https://developers.facebook.com).
3. Configurar el webhook de WhatsApp Business apuntando a `POST /message`.
4. Adaptar el endpoint para parsear el formato de payload de WhatsApp.

"""
Prueba cada endpoint de Fudo y reporta cuáles funcionan.
Uso: python test_fudo.py
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

FUDO_BASE_URL = os.environ.get("FUDO_BASE_URL", "https://api.fu.do/v1alpha1")
FUDO_AUTH_URL = "https://auth.fu.do/api"
API_KEY = os.environ.get("FUDO_API_KEY", "")
API_SECRET = os.environ.get("FUDO_API_SECRET", "")

DATE_FILTER = "and(gte.2026-06-09T00:00:00Z,lte.2026-06-11T23:59:59Z)"

ENDPOINTS = [
    ("/sales",             f"filter[createdAt]={DATE_FILTER}&page[size]=5"),
    ("/products",          "page[size]=5&filter[active]=true"),
    ("/ingredients",       "page[size]=5"),
    ("/expenses",          f"filter[createdAt]={DATE_FILTER}&page[size]=5"),
    ("/expense-categories","page[size]=5"),
    ("/payments",          f"filter[createdAt]={DATE_FILTER}&page[size]=5"),
    ("/payment-methods",   "page[size]=5"),
    ("/customers",         "page[size]=5"),
    ("/tables",            "page[size]=5"),
    ("/product-categories","page[size]=5"),
    ("/users",             "page[size]=5"),
    ("/providers",         "page[size]=5"),
    ("/rooms",             "page[size]=5"),
    ("/kitchens",          "page[size]=5"),
    ("/items",             "page[size]=5&sort=-createdAt"),
]


def authenticate() -> str:
    print("Autenticando en Fudo...")
    resp = requests.post(
        FUDO_AUTH_URL,
        json={"apiKey": API_KEY, "apiSecret": API_SECRET},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("token") or data.get("access_token") or data.get("jwt")
    if not token:
        raise ValueError(f"No se recibió token. Respuesta: {data}")
    print("Token obtenido OK\n")
    return token


def count_records(data: dict) -> int:
    """Intenta contar registros en la respuesta."""
    if isinstance(data, list):
        return len(data)
    for key in ("data", "items", "records", "results"):
        val = data.get(key)
        if isinstance(val, list):
            return len(val)
    # Si el dict tiene entries sin wrapper conocido
    if "id" in data:
        return 1
    return -1  # no se pudo determinar


def test_endpoint(token: str, endpoint: str, query: str) -> tuple[str, str]:
    """
    Retorna (status, detalle) donde status es 'ok' | 'empty' | 'error'.
    """
    url = f"{FUDO_BASE_URL}{endpoint}?{query}"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                return "error", "200 pero respuesta no es JSON"
            n = count_records(data)
            if n == 0:
                return "empty", "0 registros"
            if n > 0:
                return "ok", f"{n} registro(s)"
            # n == -1: no se pudo contar pero la respuesta llegó
            keys = list(data.keys()) if isinstance(data, dict) else "[]"
            return "ok", f"respuesta recibida (keys: {keys})"
        else:
            try:
                msg = resp.json()
            except Exception:
                msg = resp.text[:120]
            return "error", f"{resp.status_code} — {msg}"
    except requests.exceptions.RequestException as exc:
        return "error", str(exc)


def main():
    if not API_KEY or not API_SECRET:
        print("ERROR: FUDO_API_KEY o FUDO_API_SECRET no están en el .env")
        return

    try:
        token = authenticate()
    except Exception as exc:
        print(f"ERROR de autenticación: {exc}")
        return

    ok_list = []
    empty_list = []
    error_list = []

    col_w = max(len(ep) for ep, _ in ENDPOINTS) + 2

    print(f"{'ENDPOINT':<{col_w}}  RESULTADO")
    print("-" * (col_w + 40))

    for endpoint, query in ENDPOINTS:
        status, detail = test_endpoint(token, endpoint, query)
        if status == "ok":
            icon = "✅"
            ok_list.append(endpoint)
        elif status == "empty":
            icon = "⚪"
            empty_list.append(f"{endpoint} ({detail})")
        else:
            icon = "❌"
            error_list.append(f"{endpoint} ({detail})")
        print(f"{icon}  {endpoint:<{col_w}}  {detail}")

    print("\n" + "=" * 60)
    print("RESUMEN")
    print("=" * 60)

    if ok_list:
        print(f"\n✅ FUNCIONA ({len(ok_list)}): {', '.join(ok_list)}")
    if empty_list:
        print(f"\n⚪ VACÍO    ({len(empty_list)}): {', '.join(empty_list)}")
    if error_list:
        print(f"\n❌ ERROR    ({len(error_list)}): {', '.join(error_list)}")

    print()


if __name__ == "__main__":
    main()

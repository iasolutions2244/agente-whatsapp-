"""Migración manual: cifra fudo_key / fudo_secret que hoy están en texto plano
en la tabla `clientes` de Supabase.

NO se ejecuta automáticamente. Revisar y correr manualmente:
    python migrate_encrypt_credentials.py            # dry-run (no escribe nada)
    python migrate_encrypt_credentials.py --apply    # aplica los cambios

Requiere las mismas variables de entorno que main.py (SUPABASE_URL,
SUPABASE_SERVICE_KEY, ENCRYPTION_KEY) disponibles vía .env o el entorno.

Detección de texto plano: intenta descifrar cada valor con la ENCRYPTION_KEY
actual; si falla (no es un token Fernet válido), asume que está en texto plano
y lo cifra. Si ya es un token Fernet válido, lo deja igual (evita cifrar dos
veces si el script se corre más de una vez).
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

from supabase import create_client
from crypto_utils import encrypt_value, decrypt_value


def is_plaintext(valor: str | None) -> bool:
    if not valor:
        return False
    try:
        decrypt_value(valor)
        return False  # descifró OK -> ya estaba cifrado
    except ValueError:
        return True  # no es un token Fernet válido -> texto plano


def main() -> None:
    apply_changes = "--apply" in sys.argv

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("Faltan SUPABASE_URL / SUPABASE_SERVICE_KEY en el entorno")

    sb = create_client(url, key)
    res = sb.table("clientes").select("id, nombre_restaurante, fudo_key, fudo_secret").execute()

    pendientes = []
    for row in res.data or []:
        fk, fs = row.get("fudo_key"), row.get("fudo_secret")
        if is_plaintext(fk) or is_plaintext(fs):
            pendientes.append(row)

    if not pendientes:
        print("No hay credenciales en texto plano pendientes de migrar.")
        return

    print(f"Clientes con credenciales en texto plano: {len(pendientes)}")
    for row in pendientes:
        print(f"  - {row['id']} | {row.get('nombre_restaurante')}")

    if not apply_changes:
        print("\nDry-run (no se modificó nada). Volvé a correr con --apply para cifrar estos registros.")
        return

    for row in pendientes:
        payload = {}
        if is_plaintext(row.get("fudo_key")):
            payload["fudo_key"] = encrypt_value(row["fudo_key"])
        if is_plaintext(row.get("fudo_secret")):
            payload["fudo_secret"] = encrypt_value(row["fudo_secret"])
        sb.table("clientes").update(payload).eq("id", row["id"]).execute()
        print(f"  ✅ Migrado: {row['id']} | {row.get('nombre_restaurante')}")

    print("\nMigración completa.")


if __name__ == "__main__":
    main()

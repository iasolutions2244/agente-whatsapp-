"""Prueba manual rápida de crypto_utils.encrypt_value / decrypt_value.
Requiere ENCRYPTION_KEY configurada (via .env o entorno)."""

import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv()

from crypto_utils import encrypt_value, decrypt_value

original = "test_api_key_12345"
cifrado = encrypt_value(original)
descifrado = decrypt_value(cifrado)

assert cifrado != original, "El valor cifrado no debería ser igual al original"
assert descifrado == original, "El valor descifrado debería ser igual al original"
assert encrypt_value(None) is None
assert encrypt_value("") is None
assert decrypt_value(None) is None
assert decrypt_value("") is None

print("Original:  ", original)
print("Cifrado:   ", cifrado)
print("Descifrado:", descifrado)
print("✅ Cifrado funcionando correctamente")

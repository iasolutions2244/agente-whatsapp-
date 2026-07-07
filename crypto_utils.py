"""Cifrado a nivel de aplicación para credenciales sensibles (fudo_key, fudo_secret)
guardadas en Supabase. Usa Fernet (AES128 en CBC + HMAC-SHA256) de la librería
`cryptography`.
"""

import os

from cryptography.fernet import Fernet, InvalidToken

_ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY")

if not _ENCRYPTION_KEY:
    raise RuntimeError(
        "Falta la variable de entorno ENCRYPTION_KEY. "
        "Generala con: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
        "y configurala en Railway (y en tu .env local) antes de iniciar el servidor."
    )

_fernet = Fernet(_ENCRYPTION_KEY.encode())


def encrypt_value(valor: str | None) -> str | None:
    """Cifra un string en texto plano. Devuelve None si valor es None o vacío."""
    if not valor:
        return None
    return _fernet.encrypt(valor.encode("utf-8")).decode("utf-8")


def decrypt_value(valor_cifrado: str | None) -> str | None:
    """Descifra un string cifrado con encrypt_value. Devuelve None si valor_cifrado
    es None o vacío."""
    if not valor_cifrado:
        return None
    try:
        return _fernet.decrypt(valor_cifrado.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise ValueError(
            "No se pudo descifrar el valor: no es un token Fernet válido para la "
            "ENCRYPTION_KEY actual (¿credencial en texto plano sin migrar, o "
            "ENCRYPTION_KEY incorrecta?)."
        )

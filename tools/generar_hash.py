"""Genera el hash de contraseña para ADMIN_PASSWORD_HASH.  Uso: python tools/generar_hash.py"""
import getpass

from werkzeug.security import generate_password_hash

clave = getpass.getpass("Nueva contraseña: ")
if len(clave) < 12:
    raise SystemExit("Usa al menos 12 caracteres.")
if clave != getpass.getpass("Repite la contraseña: "):
    raise SystemExit("Las contraseñas no coinciden.")
print("\nCopia esta línea en tu .env (o en las variables de entorno de Render):\n")
print(f"ADMIN_PASSWORD_HASH={generate_password_hash(clave)}")

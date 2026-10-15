"""Acceso a datos de SISEM (MySQL).

Cambios respecto a la versión anterior:
  * Pool de conexiones en lugar de abrir/cerrar una conexión por consulta.
  * Los errores se registran con ``logging`` (sin ``print``) y no exponen
    detalles de la base de datos al usuario.
  * Las lecturas de catálogos devuelven ``[]`` si fallan; las escrituras y la
    lectura de un reporte lanzan la excepción para que la app responda 500.
  * Nuevas funciones para el historial y el tablero.

Las funciones públicas mantienen los mismos nombres y devuelven tuplas, para
no romper el código existente.
"""
import logging
import os
from contextlib import contextmanager

import mysql.connector
from mysql.connector import pooling
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

_pool = None


def _env(*nombres, default=None):
    """Lee la primera variable de entorno definida (admite db_host o DB_HOST)."""
    for nombre in nombres:
        valor = os.getenv(nombre)
        if valor not in (None, ''):
            return valor
    return default


def _configuracion():
    config = {
        'host': _env('db_host', 'DB_HOST', default='localhost'),
        'user': _env('db_user', 'DB_USER'),
        'password': _env('db_password', 'DB_PASSWORD'),
        'database': _env('db_name', 'DB_NAME'),
        'port': int(_env('db_port', 'DB_PORT', default='3306')),
        'charset': 'utf8mb4',
        'connection_timeout': 10,
    }
    # Para bases de datos en la nube que exigen TLS: db_ssl_ca=/ruta/ca.pem
    ssl_ca = _env('db_ssl_ca', 'DB_SSL_CA')
    if ssl_ca:
        config['ssl_ca'] = ssl_ca
    return config


def _obtener_pool():
    global _pool
    if _pool is None:
        tamano = int(_env('db_pool_size', 'DB_POOL_SIZE', default='5'))
        _pool = pooling.MySQLConnectionPool(
            pool_name='sisem_pool',
            pool_size=tamano,
            pool_reset_session=True,
            **_configuracion(),
        )
    return _pool


@contextmanager
def _cursor(commit=False):
    """Entrega un cursor y devuelve la conexión al pool al terminar."""
    conexion = _obtener_pool().get_connection()
    try:
        cursor = conexion.cursor()
        try:
            yield cursor
            if commit:
                conexion.commit()
        except Exception:
            if commit:
                conexion.rollback()
            raise
        finally:
            cursor.close()
    finally:
        conexion.close()  # en un pool, close() solo la devuelve


def _leer_catalogo(consulta, parametros=None):
    """Lectura tolerante a fallos: registra el error y devuelve []."""
    try:
        with _cursor() as cursor:
            cursor.execute(consulta, parametros or ())
            return cursor.fetchall()
    except Exception:
        logger.exception("Error consultando la base de datos")
        return []


# -----------------------------------------------------------------------------------------
# Catálogos (la columna 0 es el id y la columna 1 el nombre)
# -----------------------------------------------------------------------------------------
def servicios():
    return _leer_catalogo("SELECT * FROM mantenimientos;")


def personal():
    return _leer_catalogo("SELECT * FROM personal;")


def marcas():
    return _leer_catalogo("SELECT * FROM marcas;")


def clientes():
    return _leer_catalogo("SELECT * FROM clientes;")


def tipo_equipos():
    return _leer_catalogo("SELECT * FROM tipo_equipo;")


# -----------------------------------------------------------------------------------------
# Reportes
# -----------------------------------------------------------------------------------------
def subir_reporte(tecnico, cliente, equipo, fecha, reporte):
    """Guarda un reporte y devuelve ``(id_nuevo, equipo)``. Lanza si falla."""
    consulta = (
        "INSERT INTO reportes (id_tecnico, id_cliente, id_equipo, fecha, reporte) "
        "VALUES (%s, %s, %s, %s, %s);"
    )
    with _cursor(commit=True) as cursor:
        cursor.execute(consulta, (tecnico, cliente, equipo, fecha, reporte))
        ultimo_id = cursor.lastrowid
    logger.info("Reporte guardado con id %s", ultimo_id)
    return ultimo_id, equipo


def obtener_reporte_por_id(reporte_id):
    """Devuelve ``(id, id_tecnico, id_cliente, id_equipo, fecha, reporte)`` o None."""
    consulta = (
        "SELECT id, id_tecnico, id_cliente, id_equipo, fecha, reporte "
        "FROM reportes WHERE id = %s;"
    )
    with _cursor() as cursor:
        cursor.execute(consulta, (reporte_id,))
        return cursor.fetchone()


def listar_reportes(limite=25, desplazamiento=0):
    """Reportes más recientes primero: ``(id, id_tecnico, id_cliente, id_equipo, fecha)``."""
    return _leer_catalogo(
        "SELECT id, id_tecnico, id_cliente, id_equipo, fecha "
        "FROM reportes ORDER BY id DESC LIMIT %s OFFSET %s;",
        (int(limite), int(desplazamiento)),
    )


def contar_reportes(desde_fecha=None):
    """Total de reportes; con ``desde_fecha`` ('AAAA-MM-DD') cuenta desde ese día."""
    try:
        with _cursor() as cursor:
            if desde_fecha:
                cursor.execute("SELECT COUNT(*) FROM reportes WHERE fecha >= %s;", (desde_fecha,))
            else:
                cursor.execute("SELECT COUNT(*) FROM reportes;")
            return int(cursor.fetchone()[0])
    except Exception:
        logger.exception("Error contando reportes")
        return None

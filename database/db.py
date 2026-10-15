"""Acceso a datos de SISEM (PostgreSQL).

Conexión:
  * ``DATABASE_URL`` (la que entrega Render):  postgresql://usuario:clave@host:5432/base
  * o, si no existe, las variables sueltas ``db_host``, ``db_port``, ``db_user``,
    ``db_password`` y ``db_name``.
  * Para conexiones externas a Render añade ``?sslmode=require`` a la URL.

Las funciones públicas conservan nombres y formato de la versión MariaDB
(devuelven listas de tuplas: columna 0 = id, columna 1 = nombre).
"""
import logging
import os
from contextlib import contextmanager

from dotenv import load_dotenv
from psycopg_pool import ConnectionPool

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


def _obtener_pool():
    global _pool
    if _pool is None:
        url = _env('DATABASE_URL', default='')
        kwargs = {'connect_timeout': 10}
        if not url:
            kwargs.update({
                'host': _env('db_host', 'DB_HOST', default='localhost'),
                'port': int(_env('db_port', 'DB_PORT', default='5432')),
                'user': _env('db_user', 'DB_USER'),
                'password': _env('db_password', 'DB_PASSWORD'),
                'dbname': _env('db_name', 'DB_NAME'),
            })
        _pool = ConnectionPool(
            conninfo=url,
            kwargs=kwargs,
            min_size=1,
            max_size=int(_env('db_pool_size', 'DB_POOL_SIZE', default='5')),
            timeout=10,
            open=True,
        )
    return _pool


@contextmanager
def _cursor():
    """Cursor dentro de una transacción: confirma al salir bien y deshace si hay error."""
    with _obtener_pool().connection() as conexion:
        with conexion.cursor() as cursor:
            yield cursor


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
    return _leer_catalogo("SELECT id, tipo_mantenimiento FROM mantenimientos ORDER BY id;")


def personal():
    return _leer_catalogo("SELECT id, nombre FROM personal ORDER BY nombre;")


def marcas():
    return _leer_catalogo("SELECT id, brand FROM marcas ORDER BY brand;")


def clientes():
    return _leer_catalogo("SELECT id, nombre FROM clientes ORDER BY nombre;")


def tipo_equipos():
    return _leer_catalogo("SELECT id, equipo FROM tipo_equipo ORDER BY id;")


# -----------------------------------------------------------------------------------------
# Reportes
# -----------------------------------------------------------------------------------------
def subir_reporte(tecnico, cliente, equipo, fecha, reporte):
    """Guarda un reporte y devuelve ``(id_nuevo, equipo)``. Lanza si falla."""
    consulta = (
        "INSERT INTO reportes (id_tecnico, id_cliente, id_equipo, fecha, reporte) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id;"
    )
    with _cursor() as cursor:
        cursor.execute(consulta, (tecnico, cliente, equipo, fecha, reporte))
        ultimo_id = cursor.fetchone()[0]
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
                cursor.execute("SELECT COUNT(*) FROM reportes WHERE fecha >= %s::date;", (desde_fecha,))
            else:
                cursor.execute("SELECT COUNT(*) FROM reportes;")
            return int(cursor.fetchone()[0])
    except Exception:
        logger.exception("Error contando reportes")
        return None

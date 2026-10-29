"""Acceso a datos de SISEM (PostgreSQL).

Conexión:
  * ``DATABASE_URL`` (la que entrega Render):  postgresql://usuario:clave@host:5432/base
  * o, si no existe, las variables sueltas ``db_host``, ``db_port``, ``db_user``,
    ``db_password`` y ``db_name``.
  * Para conexiones externas a Render añade ``?sslmode=require`` a la URL.

Los catálogos y reportes conservan nombres y formato de la versión MariaDB
(devuelven listas de tuplas: columna 0 = id, columna 1 = nombre). Las funciones de
hojas de vida devuelven diccionarios {columna: valor}.
"""
import logging
import os
from contextlib import contextmanager

from dotenv import load_dotenv
from psycopg import errors, sql
from psycopg.rows import dict_row
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
def _cursor(row_factory=None):
    """Cursor dentro de una transacción: confirma al salir bien y deshace si hay error.

    Con ``row_factory=dict_row`` cada fila es un diccionario {columna: valor}.
    """
    with _obtener_pool().connection() as conexion:
        with conexion.cursor(row_factory=row_factory) as cursor:
            yield cursor


def _leer_catalogo(consulta, parametros=None, como_dict=False):
    """Lectura tolerante a fallos: registra el error y devuelve []."""
    try:
        with _cursor(dict_row if como_dict else None) as cursor:
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

def inventario():
    """Equipos del inventario con los nombres de tipo, marca y cliente (diccionarios)."""
    return _leer_catalogo(
        "SELECT i.id, te.equipo AS tipo_equipo, m.brand AS marca, i.lote, i.serie, "
        "       i.serial_inventario_interno, c.nombre AS cliente, i.riesgo, i.invima "
        "FROM inventario i "
        "LEFT JOIN tipo_equipo te ON te.id = i.id_equipo "
        "LEFT JOIN marcas m ON m.id = i.marca "
        "LEFT JOIN clientes c ON c.id = i.id_cliente "
        "ORDER BY i.serial_inventario_interno;",
        como_dict=True,
    )

def riesgo_equipos():
    return _leer_catalogo("SELECT id_riesgo, tipo_riesgo FROM riesgo ORDER BY id_riesgo;")


# -----------------------------------------------------------------------------------------
# Hojas de vida de equipos (tablas hojas_vida_equipos y hojas_vida_accesorios)
# -----------------------------------------------------------------------------------------
class HojaVidaDuplicada(Exception):
    """El equipo ya tiene hoja de vida (id_inventario es único en hojas_vida_equipos)."""


def inventario_sin_hoja_vida():
    """Equipos del inventario que todavía no tienen hoja de vida, con los datos que la completan.

    Cada fila es un diccionario: id, serie, inventario, equipo, marca, entidad, riesgo, invima.
    """
    return _leer_catalogo(
        "SELECT i.id, i.serie, i.serial_inventario_interno AS inventario, te.equipo, "
        "       m.brand AS marca, c.nombre AS entidad, i.riesgo, i.invima "
        "FROM inventario i "
        "LEFT JOIN tipo_equipo te ON te.id = i.id_equipo "
        "LEFT JOIN marcas m ON m.id = i.marca "
        "LEFT JOIN clientes c ON c.id = i.id_cliente "
        "LEFT JOIN hojas_vida_equipos hv ON hv.id_inventario = i.id "
        "WHERE hv.id IS NULL "
        "ORDER BY i.serie;",
        como_dict=True,
    )


def responsables():
    """Personal con sus datos de contacto: diccionarios id, nombre, telefono, correo."""
    return _leer_catalogo("SELECT id, nombre, telefono, correo FROM personal ORDER BY nombre;",
                          como_dict=True)


def guardar_hoja_vida(datos, accesorios):
    """Guarda la hoja de vida y sus accesorios en UNA transacción y devuelve el id nuevo.

    ``datos``: {columna de hojas_vida_equipos: valor}. Las columnas salen de una lista fija
    validada en app.py; aun así se escapan como identificadores SQL.
    ``accesorios``: [{fila, cantidad, descripcion, id_marca, modelo}].
    Lanza ``HojaVidaDuplicada`` si el equipo ya tiene hoja de vida.
    """
    insertar = sql.SQL(
        "INSERT INTO hojas_vida_equipos ({columnas}) VALUES ({valores}) "
        "ON CONFLICT (id_inventario) DO NOTHING RETURNING id;"
    ).format(
        columnas=sql.SQL(', ').join(map(sql.Identifier, datos)),
        valores=sql.SQL(', ').join(sql.Placeholder() * len(datos)),
    )
    with _cursor() as cursor:
        cursor.execute(insertar, list(datos.values()))
        fila = cursor.fetchone()
        if fila is None:
            raise HojaVidaDuplicada(datos.get('id_inventario'))
        nuevo_id = fila[0]
        if accesorios:
            cursor.executemany(
                "INSERT INTO hojas_vida_accesorios (id_hoja_vida, fila, cantidad, descripcion, id_marca, modelo) "
                "VALUES (%s, %s, %s, %s, %s, %s);",
                [(nuevo_id, a['fila'], a['cantidad'], a['descripcion'], a['id_marca'], a['modelo'])
                 for a in accesorios],
            )
    logger.info("Hoja de vida guardada con id %s", nuevo_id)
    return nuevo_id


def _patron_busqueda(texto):
    """'%texto%' para ILIKE; los % _ \\ que escriba el usuario se buscan como texto normal."""
    return '%' + texto.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


# Sin texto devuelve todas; con texto busca en serie, inventario, equipo, entidad y marca.
_FILTRO_HV = ("(%(texto)s = '' OR serie ILIKE %(patron)s OR numero_inventario ILIKE %(patron)s "
              "OR nombre_equipo ILIKE %(patron)s OR entidad ILIKE %(patron)s OR marca ILIKE %(patron)s)")


def buscar_hojas_vida(texto='', limite=20, desplazamiento=0):
    """Hojas de vida más recientes primero (sin la foto), filtradas por ``texto``."""
    return _leer_catalogo(
        "SELECT id_hoja_vida AS id, nombre_equipo, marca, modelo, serie, numero_inventario, "
        "       entidad, area_servicio, fecha_creacion "
        f"FROM v_hojas_vida WHERE {_FILTRO_HV} "
        "ORDER BY id_hoja_vida DESC LIMIT %(limite)s OFFSET %(desplazamiento)s;",
        {'texto': texto, 'patron': _patron_busqueda(texto),
         'limite': int(limite), 'desplazamiento': int(desplazamiento)},
        como_dict=True,
    )


def contar_hojas_vida(texto=''):
    """Cuántas hojas de vida coinciden con ``texto``; None si falla la consulta."""
    try:
        with _cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM v_hojas_vida WHERE {_FILTRO_HV};",
                           {'texto': texto, 'patron': _patron_busqueda(texto)})
            return int(cursor.fetchone()[0])
    except Exception:
        logger.exception("Error contando hojas de vida")
        return None


def obtener_hoja_vida(hoja_id):
    """Hoja de vida completa (vista v_hojas_vida) con la lista ``accesorios``; None si no existe."""
    with _cursor(dict_row) as cursor:
        cursor.execute("SELECT * FROM v_hojas_vida WHERE id_hoja_vida = %s;", (hoja_id,))
        hoja = cursor.fetchone()
        if hoja is None:
            return None
        cursor.execute(
            "SELECT a.fila, a.cantidad, a.descripcion, m.brand AS marca, a.modelo "
            "FROM hojas_vida_accesorios a LEFT JOIN marcas m ON m.id = a.id_marca "
            "WHERE a.id_hoja_vida = %s ORDER BY a.fila;",
            (hoja_id,),
        )
        hoja['accesorios'] = cursor.fetchall()
    return hoja


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

# ----------------------------------------INVENTARIO-------------------------------------------------

class InventarioDuplicado(Exception):
    """Ya existe un equipo con un valor que debe ser único. ``campo`` dice cuál
    ('serie', 'serial_inventario_interno', 'invima') o None si no se reconoce."""

    def __init__(self, campo=None):
        super().__init__(campo)
        self.campo = campo


# Restricción UNIQUE de PostgreSQL (nombre real en la base) -> campo repetido
_UNICOS_INVENTARIO = {
    'unico_serie': 'serie',
}


def guardar_inventario(datos):
    """Guarda un nuevo equipo en el inventario y devuelve el id nuevo.

    ``datos``: {columna de inventario: valor}. Las columnas salen de una lista fija
    validada en app.py; aun así se escapan como identificadores SQL.
    Lanza ``InventarioDuplicado`` (con el campo repetido) si choca con un UNIQUE.
    """
    insertar = sql.SQL(
        "INSERT INTO inventario ({columnas}) VALUES ({valores}) RETURNING id;"
    ).format(columnas=sql.SQL(', ').join(map(sql.Identifier, datos)),
        valores=sql.SQL(', ').join(sql.Placeholder() * len(datos)),
    )
    try:
        with _cursor() as cursor:
            cursor.execute(insertar, list(datos.values()))
            nuevo_id = cursor.fetchone()[0]
    except errors.UniqueViolation as error:
        restriccion = getattr(error.diag, 'constraint_name', None)
        raise InventarioDuplicado(_UNICOS_INVENTARIO.get(restriccion)) from error
    logger.info("Inventario guardado con id %s", nuevo_id)
    return nuevo_id
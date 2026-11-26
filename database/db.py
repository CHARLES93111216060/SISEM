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
from psycopg.types.json import Jsonb
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

def areas():
    return _leer_catalogo("SELECT id, area FROM areas_clinicas ORDER BY id;")

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

# -----------------------------------------------------------------------------------------
# Cronograma de mantenimientos (tabla cronograma_mantenimientos)
# -----------------------------------------------------------------------------------------
_CRONOGRAMA_SELECT = (
    "SELECT cm.id, cm.fecha_programada, cm.estado, cm.frecuencia_meses, cm.observaciones, "
    "       cm.id_equipo, cm.tipo_mantenimiento AS id_tipo, cm.tecnico_asignado_id AS id_tecnico, "
    "       i.serie, i.serial_inventario_interno AS inventario, i.id_cliente, te.equipo, "
    "       ma.brand AS marca, c.nombre AS cliente, m.tipo_mantenimiento AS tipo, p.nombre AS tecnico "
    "FROM cronograma_mantenimientos cm "
    "LEFT JOIN inventario i ON i.id = cm.id_equipo "
    "LEFT JOIN tipo_equipo te ON te.id = i.id_equipo "
    "LEFT JOIN marcas ma ON ma.id = i.marca "
    "LEFT JOIN clientes c ON c.id = i.id_cliente "
    "LEFT JOIN mantenimientos m ON m.id = cm.tipo_mantenimiento "
    "LEFT JOIN personal p ON p.id = cm.tecnico_asignado_id "
)


def equipos_cronograma():
    """Equipos que se pueden programar: diccionarios id, serie, inventario, equipo, id_cliente, cliente."""
    return _leer_catalogo(
        "SELECT i.id, i.serie, i.serial_inventario_interno AS inventario, te.equipo, "
        "       i.id_cliente, c.nombre AS cliente "
        "FROM inventario i "
        "LEFT JOIN tipo_equipo te ON te.id = i.id_equipo "
        "LEFT JOIN clientes c ON c.id = i.id_cliente "
        "ORDER BY c.nombre, i.serie;",
        como_dict=True,
    )


def cronograma_entre(desde, hasta, hoy, estado=None, id_tecnico=None, id_cliente=None):
    """Mantenimientos con fecha entre ``desde`` y ``hasta`` (incluidas), más antiguos primero.

    ``estado`` admite 'vencido': pendientes con fecha anterior a ``hoy``. A diferencia de los
    catálogos, si la consulta falla lanza el error: la página no debe decir "no hay nada
    programado" cuando en realidad no pudo leer la tabla.
    """
    condiciones = ["cm.fecha_programada BETWEEN %s AND %s"]
    parametros = [desde, hasta]
    if estado == 'vencido':
        condiciones.append("cm.estado = 'pendiente' AND cm.fecha_programada < %s")
        parametros.append(hoy)
    elif estado:
        condiciones.append("cm.estado = %s")
        parametros.append(estado)
    if id_tecnico:
        condiciones.append("cm.tecnico_asignado_id = %s")
        parametros.append(id_tecnico)
    if id_cliente:
        condiciones.append("i.id_cliente = %s")
        parametros.append(id_cliente)
    consulta = (_CRONOGRAMA_SELECT + "WHERE " + " AND ".join(condiciones)
                + " ORDER BY cm.fecha_programada, cm.id;")
    with _cursor(dict_row) as cursor:
        cursor.execute(consulta, parametros)
        return cursor.fetchall()


def obtener_cronograma(cronograma_id):
    """Un mantenimiento programado (diccionario) o None."""
    with _cursor(dict_row) as cursor:
        cursor.execute(_CRONOGRAMA_SELECT + "WHERE cm.id = %s;", (cronograma_id,))
        return cursor.fetchone()


def programar_mantenimientos(filas):
    """Inserta las fechas en UNA transacción; devuelve ``(creados, omitidos)``.

    ``filas``: [{id_equipo, tipo_mantenimiento, frecuencia_meses, fecha_programada,
    tecnico_asignado_id, observaciones}]. Se omite la fecha que ya esté programada para el mismo
    equipo y tipo de mantenimiento, así repetir el formulario no duplica el cronograma.
    """
    consulta = (
        "INSERT INTO cronograma_mantenimientos "
        "(id_equipo, tipo_mantenimiento, frecuencia_meses, fecha_programada, tecnico_asignado_id, observaciones) "
        "SELECT %(id_equipo)s::int, %(tipo_mantenimiento)s::int, %(frecuencia_meses)s::int, "
        "       %(fecha_programada)s::date, %(tecnico_asignado_id)s::int, %(observaciones)s::text "
        "WHERE NOT EXISTS (SELECT 1 FROM cronograma_mantenimientos "
        "                  WHERE id_equipo = %(id_equipo)s::int "
        "                    AND tipo_mantenimiento = %(tipo_mantenimiento)s::int "
        "                    AND fecha_programada = %(fecha_programada)s::date);"
    )
    creados = 0
    with _cursor() as cursor:
        for fila in filas:
            cursor.execute(consulta, fila)
            creados += cursor.rowcount
    logger.info("Cronograma: %s fechas creadas, %s ya existían", creados, len(filas) - creados)
    return creados, len(filas) - creados


def actualizar_cronograma(cronograma_id, estado, fecha_programada, id_tecnico, observaciones):
    """Actualiza un mantenimiento programado; devuelve False si no existe."""
    with _cursor() as cursor:
        cursor.execute(
            "UPDATE cronograma_mantenimientos "
            "SET estado = %s, fecha_programada = %s, tecnico_asignado_id = %s, observaciones = %s "
            "WHERE id = %s;",
            (estado, fecha_programada, id_tecnico, observaciones, cronograma_id),
        )
        return cursor.rowcount > 0


def consultar_reporte_general():
    """Reportes generales (diccionarios) con los nombres de cliente, equipo, servicio y técnico.

    Del más reciente al más antiguo. Si la base falla devuelve [] (no rompe la página).
    """
    return _leer_catalogo(
        "SELECT rg.id, rg.informacion->>'fecha' AS fecha, rg.fecha_entrega, "
        "       rg.informacion->'equipo'->>'inventario' AS inventario, "
        "       c.nombre AS cliente, rg.solicitante, rg.area, rg.ciudad, "
        "       i.serie, te.equipo AS tipo_equipo, "
        "       m.tipo_mantenimiento AS servicio, p.nombre AS tecnico "
        "FROM reporte_general rg "
        "LEFT JOIN clientes c ON c.id = rg.cliente "
        "LEFT JOIN inventario i ON i.id = rg.serie "
        "LEFT JOIN tipo_equipo te ON te.id = i.id_equipo "
        "LEFT JOIN mantenimientos m ON m.id = rg.servicio "
        "LEFT JOIN personal p ON p.id = rg.entregado_por "
        "ORDER BY rg.id DESC;",
        como_dict=True,
    )

def obtener_reporte_general(reporte_id):
    """Un reporte general (diccionario) con los nombres de cliente, servicio y técnico; None si no existe."""
    with _cursor(dict_row) as cursor:
        cursor.execute(
            "SELECT rg.id, rg.area, rg.solicitante, rg.ciudad, rg.informacion, rg.fecha_entrega, "
            "       c.nombre AS cliente, i.serie AS serie_inventario, "
            "       m.tipo_mantenimiento AS servicio, p.nombre AS tecnico, p.cargo AS tecnico_cargo "
            "FROM reporte_general rg "
            "LEFT JOIN clientes c ON c.id = rg.cliente "
            "LEFT JOIN inventario i ON i.id = rg.serie "
            "LEFT JOIN mantenimientos m ON m.id = rg.servicio "
            "LEFT JOIN personal p ON p.id = rg.entregado_por "
            "WHERE rg.id = %s;",
            (reporte_id,),
        )
        return cursor.fetchone()


def guardar_reporte_general(datos):
    """Guarda un nuevo reporte general y devuelve el id nuevo.

    ``datos`` trae las columnas de la tabla; ``informacion`` es un dict que se guarda como jsonb.
    """
    with _cursor() as cursor:
        cursor.execute(
            "INSERT INTO reporte_general "
            "(cliente, area, solicitante, ciudad, serie, servicio, informacion, fecha_entrega, entregado_por) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;",
            (datos['cliente'], datos['area'], datos['solicitante'], datos['ciudad'],
             datos['serie'], datos['servicio'], Jsonb(datos['informacion']),
             datos['fecha_entrega'], datos['entregado_por']),
        )
        return cursor.fetchone()[0]

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


# -----------------------------------------------------------------------------------------
# Talento humano: empleados (tabla personal) y acceso al aplicativo (rol "funcionario")
# Columnas de acceso: funcion, usuario, clave_hash, acceso_activo, ultimo_acceso
# (database/postgres/04_roles_y_modulos.sql). La clave NUNCA sale hacia las plantillas.
# -----------------------------------------------------------------------------------------
class UsuarioDuplicado(Exception):
    """Ya existe otro empleado con ese nombre de usuario (índice personal_usuario_key)."""


_COLUMNAS_EMPLEADO = ('nombre', 'documento', 'cargo', 'profesion', 'correo', 'telefono')
_SELECT_EMPLEADO = (
    "SELECT id, nombre, documento, cargo, profesion, correo, telefono, funcion, usuario, "
    "       acceso_activo, ultimo_acceso, (clave_hash IS NOT NULL) AS tiene_clave "
    "FROM personal "
)


def acceso_personal(usuario):
    """Datos para iniciar sesión (incluye clave_hash); None si el usuario no existe.

    El usuario no distingue mayúsculas. Lanza la excepción si la base falla (el login
    debe responder error, no "credenciales inválidas").
    """
    with _cursor(dict_row) as cursor:
        cursor.execute(
            "SELECT id, nombre, funcion, clave_hash, acceso_activo FROM personal "
            "WHERE usuario IS NOT NULL AND lower(usuario) = lower(%s);",
            (usuario,),
        )
        return cursor.fetchone()


def estado_acceso(personal_id):
    """funcion, clave_hash y acceso_activo de un empleado (para revalidar su sesión)."""
    with _cursor(dict_row) as cursor:
        cursor.execute("SELECT funcion, clave_hash, acceso_activo FROM personal WHERE id = %s;",
                       (personal_id,))
        return cursor.fetchone()


def registrar_acceso(personal_id):
    """Guarda la fecha del último inicio de sesión (si falla no impide entrar)."""
    try:
        with _cursor() as cursor:
            cursor.execute("UPDATE personal SET ultimo_acceso = now() WHERE id = %s;", (personal_id,))
    except Exception:
        logger.exception("No se pudo registrar el último acceso de %s", personal_id)


def empleados():
    """Todo el personal (diccionarios), sin la clave."""
    return _leer_catalogo(_SELECT_EMPLEADO + "ORDER BY nombre;", como_dict=True)


def obtener_empleado(empleado_id):
    with _cursor(dict_row) as cursor:
        cursor.execute(_SELECT_EMPLEADO + "WHERE id = %s;", (empleado_id,))
        return cursor.fetchone()


def guardar_empleado(datos, empleado_id=None):
    """Crea (empleado_id None) o actualiza un empleado; devuelve su id (None si no existe).

    Solo se escriben las columnas de _COLUMNAS_EMPLEADO. Un empleado nuevo entra con
    funcion 'funcionario' y sin acceso al aplicativo (lo habilita el administrador).
    """
    valores = [datos[c] for c in _COLUMNAS_EMPLEADO]
    with _cursor() as cursor:
        if empleado_id is None:
            cursor.execute(
                sql.SQL("INSERT INTO personal ({}, funcion) VALUES ({}, 'funcionario') RETURNING id;").format(
                    sql.SQL(', ').join(map(sql.Identifier, _COLUMNAS_EMPLEADO)),
                    sql.SQL(', ').join(sql.Placeholder() * len(_COLUMNAS_EMPLEADO))),
                valores,
            )
        else:
            cursor.execute(
                sql.SQL("UPDATE personal SET {} WHERE id = %s RETURNING id;").format(
                    sql.SQL(', ').join(sql.SQL("{} = %s").format(sql.Identifier(c))
                                       for c in _COLUMNAS_EMPLEADO)),
                valores + [empleado_id],
            )
        fila = cursor.fetchone()
        return fila[0] if fila else None


def configurar_acceso(empleado_id, usuario, clave_hash, activo):
    """Asigna usuario, (opcionalmente) nueva clave y estado del acceso.

    ``clave_hash`` None conserva la clave actual. Devuelve False si el empleado no existe.
    Lanza UsuarioDuplicado si el usuario ya lo tiene otra persona.
    """
    try:
        with _cursor() as cursor:
            cursor.execute(
                "UPDATE personal SET usuario = %s, clave_hash = COALESCE(%s, clave_hash), "
                "acceso_activo = %s, funcion = COALESCE(NULLIF(funcion, ''), 'funcionario') "
                "WHERE id = %s;",
                (usuario, clave_hash, activo, empleado_id),
            )
            return cursor.rowcount > 0
    except errors.UniqueViolation as error:
        raise UsuarioDuplicado() from error


def cambiar_clave(empleado_id, clave_hash):
    with _cursor() as cursor:
        cursor.execute("UPDATE personal SET clave_hash = %s WHERE id = %s;", (clave_hash, empleado_id))
        return cursor.rowcount > 0


# -----------------------------------------------------------------------------------------
# Comercial: facturación y órdenes de compra (documento + ítems en una transacción)
# Los nombres de tabla salen de este diccionario fijo, nunca del usuario.
# -----------------------------------------------------------------------------------------
_DOCUMENTOS = {
    'factura': ('facturas', 'facturas_items', 'id_factura'),
    'orden': ('ordenes_compra', 'ordenes_compra_items', 'id_orden'),
}
_COLUMNAS_ITEM = ('fila', 'descripcion', 'cantidad', 'valor_unitario', 'iva_pct')


def guardar_documento(tipo, datos, items):
    """Inserta la factura u orden (``datos``: columnas fijas validadas en app.py) y sus
    ítems en una sola transacción. Devuelve el id nuevo."""
    tabla, tabla_items, fk = _DOCUMENTOS[tipo]
    with _cursor() as cursor:
        cursor.execute(
            sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING id;").format(
                sql.Identifier(tabla),
                sql.SQL(', ').join(map(sql.Identifier, datos)),
                sql.SQL(', ').join(sql.Placeholder() * len(datos))),
            list(datos.values()),
        )
        nuevo_id = cursor.fetchone()[0]
        if items:
            cursor.executemany(
                sql.SQL("INSERT INTO {} ({}, {}) VALUES (%s, {});").format(
                    sql.Identifier(tabla_items), sql.Identifier(fk),
                    sql.SQL(', ').join(map(sql.Identifier, _COLUMNAS_ITEM)),
                    sql.SQL(', ').join(sql.Placeholder() * len(_COLUMNAS_ITEM))),
                [[nuevo_id] + [item[c] for c in _COLUMNAS_ITEM] for item in items],
            )
    logger.info("%s guardada con id %s", tipo, nuevo_id)
    return nuevo_id


def _items_documento(cursor, tipo, documento_id):
    _, tabla_items, fk = _DOCUMENTOS[tipo]
    cursor.execute(
        sql.SQL("SELECT fila, descripcion, cantidad, valor_unitario, iva_pct FROM {} "
                "WHERE {} = %s ORDER BY fila;").format(sql.Identifier(tabla_items), sql.Identifier(fk)),
        (documento_id,),
    )
    return cursor.fetchall()


def actualizar_estado_documento(tipo, documento_id, estado):
    tabla = _DOCUMENTOS[tipo][0]
    with _cursor() as cursor:
        cursor.execute(sql.SQL("UPDATE {} SET estado = %s WHERE id = %s;").format(sql.Identifier(tabla)),
                       (estado, documento_id))
        return cursor.rowcount > 0


def listar_facturas():
    return _leer_catalogo(
        "SELECT f.id, f.fecha_emision, f.fecha_vencimiento, f.estado, f.total, c.nombre AS cliente "
        "FROM facturas f LEFT JOIN clientes c ON c.id = f.id_cliente ORDER BY f.id DESC;",
        como_dict=True,
    )


def obtener_factura(factura_id):
    """Factura con datos del cliente e ítems; None si no existe."""
    with _cursor(dict_row) as cursor:
        cursor.execute(
            "SELECT f.*, c.nombre AS cliente, c.nit, c.direccion, c.telefono, c.correo, "
            "       c.ciudad, c.departamento "
            "FROM facturas f LEFT JOIN clientes c ON c.id = f.id_cliente WHERE f.id = %s;",
            (factura_id,),
        )
        factura = cursor.fetchone()
        if factura:
            factura['items'] = _items_documento(cursor, 'factura', factura_id)
        return factura


def listar_ordenes():
    return _leer_catalogo(
        "SELECT id, proveedor, nit_proveedor, fecha, fecha_entrega, estado, total "
        "FROM ordenes_compra ORDER BY id DESC;",
        como_dict=True,
    )


def obtener_orden(orden_id):
    with _cursor(dict_row) as cursor:
        cursor.execute("SELECT * FROM ordenes_compra WHERE id = %s;", (orden_id,))
        orden = cursor.fetchone()
        if orden:
            orden['items'] = _items_documento(cursor, 'orden', orden_id)
        return orden


# -----------------------------------------------------------------------------------------
# Talento humano: cuentas de cobro y nómina
# -----------------------------------------------------------------------------------------
_EMPLEADO_DOC = ("p.nombre AS empleado, p.documento, p.cargo, p.correo, p.telefono, p.profesion")


def guardar_cuenta_cobro(datos):
    with _cursor() as cursor:
        cursor.execute(
            "INSERT INTO cuentas_cobro (id_personal, fecha, periodo_inicio, periodo_fin, concepto, "
            "valor, observaciones, creado_por) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;",
            (datos['id_personal'], datos['fecha'], datos['periodo_inicio'], datos['periodo_fin'],
             datos['concepto'], datos['valor'], datos['observaciones'], datos['creado_por']),
        )
        return cursor.fetchone()[0]


def listar_cuentas_cobro(id_personal=None):
    """Todas (admin) o solo las de un empleado (funcionario)."""
    filtro, parametros = ("WHERE cc.id_personal = %s ", (id_personal,)) if id_personal else ("", ())
    return _leer_catalogo(
        "SELECT cc.id, cc.fecha, cc.periodo_inicio, cc.periodo_fin, cc.concepto, cc.valor, cc.estado, "
        "       cc.id_personal, p.nombre AS empleado "
        "FROM cuentas_cobro cc LEFT JOIN personal p ON p.id = cc.id_personal "
        + filtro + "ORDER BY cc.id DESC;",
        parametros, como_dict=True,
    )


def obtener_cuenta_cobro(cuenta_id):
    with _cursor(dict_row) as cursor:
        cursor.execute(
            "SELECT cc.*, " + _EMPLEADO_DOC + " FROM cuentas_cobro cc "
            "LEFT JOIN personal p ON p.id = cc.id_personal WHERE cc.id = %s;",
            (cuenta_id,),
        )
        return cursor.fetchone()


def actualizar_estado_cuenta(cuenta_id, estado, observaciones):
    with _cursor() as cursor:
        cursor.execute("UPDATE cuentas_cobro SET estado = %s, observaciones = %s WHERE id = %s;",
                       (estado, observaciones, cuenta_id))
        return cursor.rowcount > 0


class NominaDuplicada(Exception):
    """El empleado ya tiene liquidada la nómina de ese mes (restricción nomina_unica)."""


_COLUMNAS_NOMINA = ('id_personal', 'periodo', 'salario_base', 'dias_trabajados', 'auxilio_transporte',
                    'horas_extra', 'bonificaciones', 'salud', 'pension', 'otras_deducciones',
                    'total_devengado', 'total_deducciones', 'neto_pagar', 'observaciones', 'creado_por')


def guardar_nomina(datos):
    try:
        with _cursor() as cursor:
            cursor.execute(
                sql.SQL("INSERT INTO nomina ({}) VALUES ({}) RETURNING id;").format(
                    sql.SQL(', ').join(map(sql.Identifier, _COLUMNAS_NOMINA)),
                    sql.SQL(', ').join(sql.Placeholder() * len(_COLUMNAS_NOMINA))),
                [datos[c] for c in _COLUMNAS_NOMINA],
            )
            return cursor.fetchone()[0]
    except errors.UniqueViolation as error:
        raise NominaDuplicada() from error


def listar_nomina(id_personal=None):
    filtro, parametros = ("WHERE n.id_personal = %s ", (id_personal,)) if id_personal else ("", ())
    return _leer_catalogo(
        "SELECT n.id, n.periodo, n.dias_trabajados, n.total_devengado, n.total_deducciones, "
        "       n.neto_pagar, n.id_personal, p.nombre AS empleado "
        "FROM nomina n LEFT JOIN personal p ON p.id = n.id_personal "
        + filtro + "ORDER BY n.periodo DESC, p.nombre;",
        parametros, como_dict=True,
    )


def obtener_nomina(nomina_id):
    with _cursor(dict_row) as cursor:
        cursor.execute(
            "SELECT n.*, " + _EMPLEADO_DOC + " FROM nomina n "
            "LEFT JOIN personal p ON p.id = n.id_personal WHERE n.id = %s;",
            (nomina_id,),
        )
        return cursor.fetchone()


# -----------------------------------------------------------------------------------------
# Laboratorio: metrología (cronograma y reportes de calibración / verificación)
# -----------------------------------------------------------------------------------------
class MetrologiaDuplicada(Exception):
    """Ya está programada la misma actividad para ese equipo y fecha."""


_SELECT_METROLOGIA = (
    "SELECT mc.id, mc.id_equipo, mc.tipo, mc.fecha_programada, mc.estado, mc.proveedor, "
    "       mc.id_responsable, mc.observaciones, i.serie, i.serial_inventario_interno AS inventario, "
    "       te.equipo, c.nombre AS cliente, p.nombre AS responsable "
    "FROM metrologia_cronograma mc "
    "JOIN inventario i ON i.id = mc.id_equipo "
    "LEFT JOIN tipo_equipo te ON te.id = i.id_equipo "
    "LEFT JOIN clientes c ON c.id = i.id_cliente "
    "LEFT JOIN personal p ON p.id = mc.id_responsable "
)


def programar_metrologia(datos):
    try:
        with _cursor() as cursor:
            cursor.execute(
                "INSERT INTO metrologia_cronograma (id_equipo, tipo, fecha_programada, proveedor, "
                "id_responsable, observaciones) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;",
                (datos['id_equipo'], datos['tipo'], datos['fecha_programada'], datos['proveedor'],
                 datos['id_responsable'], datos['observaciones']),
            )
            return cursor.fetchone()[0]
    except errors.UniqueViolation as error:
        raise MetrologiaDuplicada() from error


def metrologia_entre(desde, hasta, estado=None):
    """Actividades programadas entre dos fechas. Lanza la excepción si la base falla
    (la página debe decir que hubo un error, no que no hay nada programado)."""
    consulta = _SELECT_METROLOGIA + "WHERE mc.fecha_programada BETWEEN %s AND %s "
    parametros = [desde, hasta]
    if estado:
        consulta += "AND mc.estado = %s "
        parametros.append(estado)
    with _cursor(dict_row) as cursor:
        cursor.execute(consulta + "ORDER BY mc.fecha_programada, mc.id;", parametros)
        return cursor.fetchall()


def obtener_metrologia(metrologia_id):
    with _cursor(dict_row) as cursor:
        cursor.execute(_SELECT_METROLOGIA + "WHERE mc.id = %s;", (metrologia_id,))
        return cursor.fetchone()


def metrologia_por_realizar():
    """Actividades pendientes o en proceso (para enlazarlas desde el reporte)."""
    return _leer_catalogo(_SELECT_METROLOGIA + "WHERE mc.estado IN ('pendiente', 'en_proceso') "
                          "ORDER BY mc.fecha_programada;", como_dict=True)


def actualizar_metrologia(metrologia_id, datos):
    with _cursor() as cursor:
        cursor.execute(
            "UPDATE metrologia_cronograma SET estado = %s, fecha_programada = %s, proveedor = %s, "
            "id_responsable = %s, observaciones = %s WHERE id = %s;",
            (datos['estado'], datos['fecha_programada'], datos['proveedor'], datos['id_responsable'],
             datos['observaciones'], metrologia_id),
        )
        return cursor.rowcount > 0


def guardar_reporte_metrologia(datos):
    """Guarda el reporte y, si viene de una actividad programada del mismo equipo, la marca
    como realizada en la misma transacción. Devuelve el id del reporte."""
    try:
        with _cursor() as cursor:
            cursor.execute(
                "INSERT INTO metrologia_reportes (id_equipo, id_cronograma, tipo, fecha, id_tecnico, patron, "
                "certificado_patron, magnitud, unidad, mediciones, resultado, proxima_fecha, observaciones, "
                "creado_por) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;",
                (datos['id_equipo'], datos['id_cronograma'], datos['tipo'], datos['fecha'], datos['id_tecnico'],
                 datos['patron'], datos['certificado_patron'], datos['magnitud'], datos['unidad'],
                 Jsonb(datos['mediciones']), datos['resultado'], datos['proxima_fecha'],
                 datos['observaciones'], datos['creado_por']),
            )
            nuevo_id = cursor.fetchone()[0]
            if datos['id_cronograma']:
                cursor.execute(
                    "UPDATE metrologia_cronograma SET estado = 'realizado' "
                    "WHERE id = %s AND id_equipo = %s;",
                    (datos['id_cronograma'], datos['id_equipo']),
                )
            return nuevo_id
    except errors.UniqueViolation as error:
        raise MetrologiaDuplicada() from error


_SELECT_REPORTE_METROLOGIA = (
    "SELECT r.*, i.serie, i.serial_inventario_interno AS inventario, i.lote, te.equipo, m.brand AS marca, "
    "       c.nombre AS cliente, p.nombre AS tecnico, p.cargo AS tecnico_cargo "
    "FROM metrologia_reportes r "
    "JOIN inventario i ON i.id = r.id_equipo "
    "LEFT JOIN tipo_equipo te ON te.id = i.id_equipo "
    "LEFT JOIN marcas m ON m.id = i.marca "
    "LEFT JOIN clientes c ON c.id = i.id_cliente "
    "LEFT JOIN personal p ON p.id = r.id_tecnico "
)


def listar_reportes_metrologia():
    return _leer_catalogo(_SELECT_REPORTE_METROLOGIA + "ORDER BY r.id DESC;", como_dict=True)


def obtener_reporte_metrologia(reporte_id):
    with _cursor(dict_row) as cursor:
        cursor.execute(_SELECT_REPORTE_METROLOGIA + "WHERE r.id = %s;", (reporte_id,))
        return cursor.fetchone()


# -----------------------------------------------------------------------------------------
# Panel principal: pendientes por módulo (tolerante: si una tabla falta, ese dato sale vacío)
# -----------------------------------------------------------------------------------------
def resumen_pendientes(id_personal=None):
    """{'facturas', 'cuentas', 'metrologia_vencida', 'ordenes'} -> número o None si falla."""
    consultas = {
        'facturas': ("SELECT count(*) FROM facturas WHERE estado = 'pendiente';", ()),
        'ordenes': ("SELECT count(*) FROM ordenes_compra WHERE estado IN ('pendiente', 'aprobada');", ()),
        'metrologia_vencida': ("SELECT count(*) FROM metrologia_cronograma "
                               "WHERE estado = 'pendiente' AND fecha_programada < CURRENT_DATE;", ()),
        'cuentas': (("SELECT count(*) FROM cuentas_cobro WHERE estado = 'pendiente' AND id_personal = %s;",
                     (id_personal,)) if id_personal else
                    ("SELECT count(*) FROM cuentas_cobro WHERE estado = 'pendiente';", ())),
    }
    resumen = {}
    for clave, (consulta, parametros) in consultas.items():
        filas = _leer_catalogo(consulta, parametros)
        resumen[clave] = filas[0][0] if filas else None
    return resumen
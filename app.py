"""SISEM - Sistema Integrado de Seguimiento de Equipos Médicos (Flask)."""
import calendar
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import time
import unicodedata
import zipfile
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps
from itertools import zip_longest

import openpyxl
from dotenv import load_dotenv
from PIL import Image, ImageOps

load_dotenv()

from flask import (Flask, abort, jsonify, redirect, render_template, request,  # noqa: E402
                send_file, session, url_for)
from werkzeug.datastructures import MultiDict  # noqa: E402
from werkzeug.exceptions import HTTPException  # noqa: E402
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

from database import db  # noqa: E402
from informes import informes  # noqa: E402

logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger('sisem')


def _bool(nombre, por_defecto=False):
    valor = os.getenv(nombre)
    if valor is None or valor == '':
        return por_defecto
    return valor.strip().lower() in ('1', 'true', 'yes', 'si', 'sí', 'on')


DEBUG = _bool('FLASK_DEBUG', False)          # por defecto APAGADO (antes era True)

# ----------------------------------------------------------------------------------------
# Configuración y seguridad de la sesión
# ----------------------------------------------------------------------------------------
sisem = Flask(__name__)

_clave = os.getenv('SECRET_KEY')
if not _clave:
    if DEBUG:
        _clave = secrets.token_hex(32)
        logger.warning("SECRET_KEY no definido: se usa una clave aleatoria (las sesiones "
                       "se pierden al reiniciar). Defínela en .env.")
    else:
        raise RuntimeError("Falta la variable de entorno SECRET_KEY. Genera una con: "
                           "python -c \"import secrets; print(secrets.token_urlsafe(48))\"")

try:
    _minutos_sesion = max(1, int(os.getenv('SESSION_TIMEOUT', '15')))
except ValueError:
    _minutos_sesion = 15

sisem.config.update(
    SECRET_KEY=_clave,
    SESSION_COOKIE_NAME='sisem_session',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    # Secure exige HTTPS: activo en producción, apagado solo en modo debug local.
    SESSION_COOKIE_SECURE=_bool('SESSION_COOKIE_SECURE', not DEBUG),
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=_minutos_sesion),
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,      # los reportes son JSON pequeños
    JSON_AS_ASCII=False,
)

# Detrás del proxy de Render hay que confiar en X-Forwarded-* (IP real y https).
if _bool('TRUST_PROXY', False):
    sisem.wsgi_app = ProxyFix(sisem.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ----------------------------------------------------------------------------------------
# Credenciales (un usuario administrador definido por entorno)
# ----------------------------------------------------------------------------------------
ADMIN_USER = os.getenv('ADMIN_USER') or os.getenv('DEFAULT_USER') or ''
_hash_admin = os.getenv('ADMIN_PASSWORD_HASH')
if not _hash_admin and os.getenv('DEFAULT_PASSWORD'):
    # Compatibilidad con el .env anterior: la clave en texto plano se convierte en hash
    # al arrancar, pero conviene definir ADMIN_PASSWORD_HASH (ver README).
    _hash_admin = generate_password_hash(os.getenv('DEFAULT_PASSWORD'))
    logger.warning("Se usa DEFAULT_PASSWORD en texto plano. Define ADMIN_PASSWORD_HASH "
                   "y elimina DEFAULT_PASSWORD del .env.")
if _hash_admin:
    _hash_admin = _hash_admin.strip().strip('"').strip("'")
    try:
        check_password_hash(_hash_admin, 'prueba')      # solo valida el formato
    except Exception:
        logger.error("ADMIN_PASSWORD_HASH tiene un formato inválido (debe empezar por "
                     "'scrypt:32768:8:1$' o 'pbkdf2:sha256:...$'). Nadie podrá iniciar sesión "
                     "hasta corregirlo; genera uno nuevo con tools/generar_hash.py.")
        _hash_admin = None
if not (ADMIN_USER and _hash_admin):
    logger.error("No hay credenciales configuradas (ADMIN_USER/ADMIN_PASSWORD_HASH): "
                 "nadie podrá iniciar sesión.")
ADMIN_HASH = _hash_admin
_HASH_FALSO = generate_password_hash(secrets.token_hex(16))   # iguala tiempos de respuesta

# Límite de intentos de login por IP (en memoria; por proceso de gunicorn).
MAX_INTENTOS = 5
VENTANA_SEGUNDOS = 15 * 60
_fallos = defaultdict(deque)


def _intentos_recientes(clave):
    ahora = time.monotonic()
    cola = _fallos[clave]
    while cola and ahora - cola[0] > VENTANA_SEGUNDOS:
        cola.popleft()
    if not cola:
        _fallos.pop(clave, None)
    return len(cola)


def _registrar_fallo(clave):
    if len(_fallos) > 5000:          # evita crecimiento sin límite
        _fallos.clear()
    _fallos[clave].append(time.monotonic())


# ----------------------------------------------------------------------------------------
# CSRF: token en la sesión, enviado en formularios (campo) o AJAX (cabecera)
# ----------------------------------------------------------------------------------------
def csrf_token():
    token = session.get('_csrf')
    if not token:
        token = session['_csrf'] = secrets.token_urlsafe(32)
    return token


sisem.jinja_env.globals['csrf_token'] = csrf_token


@sisem.before_request
def _proteger_csrf():
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        enviado = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
        esperado = session.get('_csrf', '')
        if not esperado or not hmac.compare_digest(enviado.encode(), esperado.encode()):
            logger.warning("CSRF rechazado en %s desde %s", request.path, request.remote_addr)
            return _error(400, "La sesión de seguridad expiró. Recarga la página e inténtalo de nuevo.")


# ----------------------------------------------------------------------------------------
# Cabeceras de seguridad
# ----------------------------------------------------------------------------------------
_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")


@sisem.after_request
def _cabeceras_seguridad(respuesta):
    h = respuesta.headers
    h.setdefault('Content-Security-Policy', _CSP)
    h.setdefault('X-Content-Type-Options', 'nosniff')
    h.setdefault('X-Frame-Options', 'DENY')
    h.setdefault('Referrer-Policy', 'same-origin')
    h.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    if request.is_secure:
        h.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    if request.endpoint != 'static':
        h['Cache-Control'] = 'no-store'      # nada de páginas con datos en caché
    return respuesta


# ----------------------------------------------------------------------------------------
# Errores
# ----------------------------------------------------------------------------------------
def _quiere_json():
    return (request.is_json
            or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or request.path.startswith('/load_form/')
            or request.path.endswith('/guardar_informe'))


def _error(codigo, mensaje):
    if _quiere_json():
        return jsonify({"error": mensaje}), codigo
    return render_template('error.html', codigo=codigo, mensaje=mensaje), codigo


@sisem.errorhandler(HTTPException)
def _manejar_http(error):
    mensajes = {
        400: "La solicitud no es válida.",
        403: "No tienes permiso para ver esta página.",
        404: "No encontramos lo que buscas.",
        405: "Acción no permitida.",
        413: "El contenido enviado es demasiado grande.",
        429: "Demasiadas solicitudes. Espera un momento.",
    }
    return _error(error.code or 500, mensajes.get(error.code, "Ocurrió un problema."))


@sisem.errorhandler(Exception)
def _manejar_inesperado(error):
    logger.exception("Error no controlado en %s", request.path)
    return _error(500, "Ocurrió un error interno. Inténtalo de nuevo más tarde.")


# ----------------------------------------------------------------------------------------
# Autenticación y roles
#   · admin: el usuario de las variables de entorno (ADMIN_USER / ADMIN_PASSWORD_HASH).
#   · funcionario: cada persona de la tabla personal con funcion = 'funcionario', usuario,
#     clave y acceso_activo = true (lo habilita el admin en Talento humano → Roles y permisos).
# ----------------------------------------------------------------------------------------
ROL_ADMIN, ROL_FUNCIONARIO = 'admin', 'funcionario'
ROLES = {ROL_ADMIN: 'Administrador', ROL_FUNCIONARIO: 'Funcionario'}


def _huella(clave_hash):
    """Resumen corto de la clave guardada: si el admin la cambia, las sesiones abiertas caen."""
    return hashlib.sha256((clave_hash or '').encode()).hexdigest()[:16]


def _sesion_vigente():
    """True si la sesión tiene un rol válido. Para un funcionario se vuelve a consultar la base:
    si el admin le quitó el acceso o le cambió la clave, la sesión deja de servir de inmediato."""
    rol = session.get('rol')
    if 'username' not in session or rol not in ROLES:
        return False
    if rol == ROL_ADMIN:
        return True
    try:
        estado = db.estado_acceso(session.get('personal_id'))
    except Exception:
        logger.exception("No se pudo revalidar la sesión del funcionario %s", session.get('personal_id'))
        return False
    return bool(estado and estado.get('acceso_activo') and estado.get('funcion') == ROL_FUNCIONARIO
                and estado.get('clave_hash')
                and hmac.compare_digest(_huella(estado['clave_hash']), session.get('huella', '')))


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _sesion_vigente():
            session.clear()
            if _quiere_json():
                return jsonify({"error": "not authenticated"}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def rol_requerido(*roles):
    """Se pone DEBAJO de @login_required: responde 403 si el rol de la sesión no está en ``roles``."""
    def decorador(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if session.get('rol') not in roles:
                logger.warning("Acceso denegado a %s para %s (%s)", request.path,
                               session.get('username'), session.get('rol'))
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorador


solo_admin = rol_requerido(ROL_ADMIN)


def es_admin():
    return session.get('rol') == ROL_ADMIN


@sisem.context_processor
def _datos_de_sesion():
    """Disponibles en todas las plantillas (menú lateral, cabecera, botones según el rol)."""
    return {'es_admin': es_admin(), 'rol_nombre': ROLES.get(session.get('rol'), ''),
            'nombre_usuario': session.get('nombre') or session.get('username', '')}


@sisem.route('/healthz')
def healthz():
    """Chequeo de salud para Render (sin sesión ni base de datos)."""
    return jsonify({"status": "ok"})


@sisem.route('/')
def home():
    return redirect(url_for('login'))


@sisem.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = request.remote_addr or 'desconocida'
        if _intentos_recientes(ip) >= MAX_INTENTOS:
            logger.warning("Login bloqueado temporalmente para %s", ip)
            return render_template('login.html',
                                   error='Demasiados intentos fallidos. Espera unos minutos.'), 429

        usuario = request.form.get('username', '').strip()[:60]
        clave = request.form.get('password', '')[:128]
        es_usuario_admin = bool(ADMIN_USER) and hmac.compare_digest(usuario.encode(), ADMIN_USER.encode())

        if es_usuario_admin:
            clave_ok = check_password_hash(ADMIN_HASH or _HASH_FALSO, clave)   # siempre se evalúa
            if clave_ok and ADMIN_HASH:
                _iniciar_sesion(usuario, ROL_ADMIN, nombre=usuario)
                _fallos.pop(ip, None)
                logger.info("Inicio de sesión del administrador %s", usuario)
                return redirect(url_for('dashboard'))
        else:
            try:
                fila = db.acceso_personal(usuario) if usuario else None
            except Exception:
                logger.exception("No se pudo consultar el acceso del personal")
                return render_template('login.html',
                                       error='No se pudo verificar el acceso. Inténtalo más tarde.'), 503
            guardada = (fila or {}).get('clave_hash')
            clave_ok = check_password_hash(guardada or _HASH_FALSO, clave)    # mismo tiempo exista o no
            if (clave_ok and guardada and fila.get('acceso_activo')
                    and fila.get('funcion') == ROL_FUNCIONARIO):
                _iniciar_sesion(usuario, ROL_FUNCIONARIO, nombre=fila['nombre'],
                                personal_id=fila['id'], huella=_huella(guardada))
                db.registrar_acceso(fila['id'])
                _fallos.pop(ip, None)
                logger.info("Inicio de sesión del funcionario %s (id %s)", usuario, fila['id'])
                return redirect(url_for('dashboard'))

        _registrar_fallo(ip)
        logger.warning("Credenciales inválidas desde %s", ip)
        return render_template('login.html', error='Credenciales inválidas'), 401

    if _sesion_vigente():
        return redirect(url_for('dashboard'))
    return render_template('login.html')


def _iniciar_sesion(usuario, rol, nombre, personal_id=None, huella=None):
    session.clear()                 # sesión nueva: evita fijación de sesión
    session.update(username=usuario, rol=rol, nombre=nombre)
    if personal_id is not None:
        session.update(personal_id=personal_id, huella=huella)
    session.permanent = True


@sisem.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


# ----------------------------------------------------------------------------------------
# Páginas
# ----------------------------------------------------------------------------------------
@sisem.route('/dashboard')
@login_required
def dashboard():
    hoy = date.today()
    estadisticas = {
        'reportes_total': db.contar_reportes(),
        'reportes_mes': db.contar_reportes(hoy.replace(day=1).isoformat()),
        'clientes': len(db.clientes() or []),
    }
    # El funcionario solo ve el conteo de SUS cuentas de cobro pendientes
    pendientes = db.resumen_pendientes(None if es_admin() else session.get('personal_id'))
    return render_template('dashboard.html', est=estadisticas, pend=pendientes)


@sisem.route('/clientes')
@login_required
def ver_clientes():
    return render_template('clientes.html', clientes=db.clientes() or [])


# Módulos del menú que aún no existen: muestran una página clara en lugar de un 404.
# Hoy todos los módulos del menú están implementados; para anunciar uno nuevo, agrégalo así:
#   ('modulo', 'seccion'): 'Título visible',
PROXIMAMENTE = {}


@sisem.route('/<modulo>/<seccion>')
@login_required
def modulo_pendiente(modulo, seccion):
    titulo = PROXIMAMENTE.get((modulo, seccion))
    if not titulo:
        abort(404)
    return render_template('proximamente.html', titulo=titulo)


# ----------------------------------------------------------------------------------------
# Mantenimiento: formulario, guardado, descarga e historial
# ----------------------------------------------------------------------------------------
@sisem.route('/mantenimiento/reportes')
@login_required
def mantenimiento_correctivo():
    return render_template('forms/forms_mantenimiento.html')


FORMULARIOS = {
    'reporte_general': 'forms/form_reporte_general.html',
    'Protocolo_de_mantenimiento': 'forms/form_protocolo_mantenimiento.html',
    'consulta_reporte': 'forms/form_consulta.html',
}
for _id, _tipo in informes.TIPOS_EQUIPO.items():
    FORMULARIOS[str(_id)] = f"forms/protocolos_mantenimiento/{_tipo['formulario']}"


@sisem.route('/load_form/<form_type>')
@login_required
def load_form(form_type):
    plantilla = FORMULARIOS.get(form_type)     # lista blanca: no se usa texto del usuario
    if not plantilla:
        return jsonify({"error": "Tipo de formulario no encontrado"}), 404
    return render_template(
        plantilla,
        tecnicos=db.personal() or [],
        brand=db.marcas() or [],
        servicios=db.servicios() or [],
        clientes=db.clientes() or [],
        tipo_equipos=db.tipo_equipos() or [],
        areas=db.areas() or [],
    )


_FECHA = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_MAX_CAMPOS = 400
_MAX_TEXTO = 5000


def _validar_informe(datos):
    """Devuelve (datos_limpios, error). Solo acepta texto y listas de texto."""
    if not isinstance(datos, dict) or not datos:
        return None, "No se recibieron datos."
    if len(datos) > _MAX_CAMPOS:
        return None, "El informe tiene demasiados campos."

    limpios = {}
    for clave, valor in datos.items():
        if not isinstance(clave, str) or not clave or len(clave) > 300:
            return None, "Hay un nombre de campo inválido."
        if isinstance(valor, list):
            if len(valor) > 100 or not all(isinstance(v, str) and len(v) <= _MAX_TEXTO for v in valor):
                return None, f"Valor inválido en «{clave[:60]}»."
        elif not isinstance(valor, str) or len(valor) > _MAX_TEXTO:
            return None, f"Valor inválido en «{clave[:60]}»."
        # PostgreSQL rechaza el carácter NUL en columnas de texto
        limpios[clave] = ([v.replace('\x00', '') for v in valor] if isinstance(valor, list)
                          else valor.replace('\x00', ''))

    for clave in ("tecnico.nombre", "cliente.nombre", "equipo.tipo", "general.fecha_mantenimiento"):
        if not isinstance(limpios.get(clave), str) or not limpios[clave].strip():
            return None, f"Falta el campo obligatorio: {clave}"

    for clave in ("tecnico.nombre", "cliente.nombre", "equipo.tipo"):
        if not limpios[clave].strip().isdigit():
            return None, f"Valor inválido en {clave}"
    if int(limpios["equipo.tipo"]) not in informes.TIPOS_EQUIPO:
        return None, "Tipo de equipo no reconocido."

    fecha = limpios["general.fecha_mantenimiento"].strip()
    try:
        if not _FECHA.match(fecha):
            raise ValueError
        datetime.strptime(fecha, '%Y-%m-%d')
    except ValueError:
        return None, "La fecha de mantenimiento no es válida."
    limpios["general.fecha_mantenimiento"] = fecha
    return limpios, None


@sisem.route('/mantenimiento/reportes/guardar_informe', methods=['POST'])
@login_required
def guardar_informe():
    datos, problema = _validar_informe(request.get_json(silent=True))
    if problema:
        return jsonify({"error": problema}), 400

    try:
        reporte_id, equipo = db.subir_reporte(
            int(datos["tecnico.nombre"]), int(datos["cliente.nombre"]), int(datos["equipo.tipo"]),
            datos["general.fecha_mantenimiento"], json.dumps(datos, ensure_ascii=False),
        )
    except Exception:
        logger.exception("No se pudo guardar el informe")
        return jsonify({"error": "No se pudo guardar el informe. Verifica los datos e inténtalo de nuevo."}), 500

    return jsonify({"mensaje": "Informe guardado correctamente", "id": reporte_id, "equipo": equipo}), 201


@sisem.route('/mantenimiento/reportes/<int:report_id>/descargar')
@sisem.route('/mantenimiento/reportes/<int:report_id>/descargar/<int:equipo>')   # ruta anterior
@login_required
def descargar_informe(report_id, equipo=None):
    """Descarga el PDF. El tipo de equipo se toma del propio reporte (no de la URL)."""
    try:
        pdf = informes.generar_reporte_pdf(report_id)
    except informes.ReporteNoEncontrado:
        abort(404)
    except informes.TipoNoSoportado:
        return _error(422, "Este tipo de equipo todavía no tiene formato de PDF.")
    except Exception:
        logger.exception("Error generando el PDF del reporte %s", report_id)
        return _error(500, "No se pudo generar el PDF. Inténtalo de nuevo más tarde.")

    return send_file(io.BytesIO(pdf), mimetype='application/pdf', as_attachment=True,
                     download_name=f"reporte_{report_id}.pdf")


POR_PAGINA = 20


@sisem.route('/mantenimiento/historial')
@login_required
def historial():
    pagina = max(1, request.args.get('pagina', 1, type=int))
    total = db.contar_reportes() or 0
    filas = db.listar_reportes(POR_PAGINA, (pagina - 1) * POR_PAGINA) if total else []

    tecnicos = {str(r[0]): r[1] for r in db.personal() or []}
    clientes = {str(r[0]): r[1] for r in db.clientes() or []}
    reportes = [{
        'id': f[0],
        'tecnico': tecnicos.get(str(f[1]), '—'),
        'cliente': clientes.get(str(f[2]), '—'),
        'equipo': informes.TIPOS_EQUIPO.get(int(f[3]), {}).get('titulo', '—'),
        'fecha': informes.fecha_legible(f[4]),
    } for f in filas]

    paginas = max(1, -(-total // POR_PAGINA))
    return render_template('historial.html', reportes=reportes, pagina=pagina,
                        paginas=paginas, total=total)

# ----------------------------------------------------------------------------------------
# Módulo de hojas de vida de equipos
# ----------------------------------------------------------------------------------------
@sisem.route('/mantenimiento/hojas_vida_equipos')
@login_required
def hojas_vida_equipos():
    return render_template('forms/hojas_de_vida_equipos.html')

@sisem.route('/crear_hv')
@login_required
def crear_hv():
    return render_template(
        'forms/hv_equipos/crear_hoja_vida.html',
        equipos=db.inventario_sin_hoja_vida() or [],
        responsables=db.responsables() or [],
        brand=db.marcas() or [],
    )


# Columnas de texto de hojas_vida_equipos -> longitud máxima (igual que en la tabla).
# El nombre del campo del formulario es el mismo nombre de la columna.
_HV_TEXTO = {
    'modelo': 120, 'registro_invima': 120, 'area_servicio': 120, 'cubiculo': 60,
    'forma_adquisicion': 60, 'vida_util': 60,
    'voltaje': 60, 'corriente': 60, 'frecuencia': 60, 'potencia': 60, 'peso': 60,
    'presion': 60, 'temperatura': 60, 'dimensiones': 120, 'capacidad': 120,
    'clasificacion_biomedica': 80, 'fuente_alimentacion': 60, 'tecnologia_predominante': 60,
}
# Columnas con valores fijos (los mismos CHECK de la tabla)
_SI_NO = frozenset({'si', 'no'})
_HV_OPCIONES = {
    'periodicidad': frozenset({'Mensual', 'Bimestral', 'Trimestral', 'Cuatrimestral', 'Semestral', 'Anual'}),
    'control_metrologico': _SI_NO,
    'manual_operacion': _SI_NO, 'manual_servicio': _SI_NO, 'ficha_tecnica': _SI_NO, 'guia_rapida': _SI_NO,
}
_HV_FILAS_ACCESORIOS = 8
_FOTO_LADO_MAX = 1200        # px; la foto se reduce y se guarda como JPEG


def _procesar_foto(archivo):
    """Valida que sea JPG/PNG real y la vuelve a codificar como JPEG.

    Reescribir la imagen elimina metadatos (EXIF con GPS, etc.) y cualquier contenido que
    no sea imagen. Devuelve ``(bytes, 'image/jpeg')``; lanza ValueError si no es válida.
    """
    try:
        imagen = Image.open(archivo.stream)
        if imagen.format not in ('JPEG', 'PNG'):
            raise ValueError
        imagen.load()
        imagen = ImageOps.exif_transpose(imagen)       # respeta la orientación de la cámara
    except (ValueError, OSError, Image.DecompressionBombError):
        raise ValueError("La foto debe ser una imagen JPG o PNG válida.")

    if imagen.mode != 'RGB':                           # transparencias sobre fondo blanco
        imagen = imagen.convert('RGBA')
        fondo = Image.new('RGB', imagen.size, 'white')
        fondo.paste(imagen, mask=imagen.getchannel('A'))
        imagen = fondo
    imagen.thumbnail((_FOTO_LADO_MAX, _FOTO_LADO_MAX))
    salida = io.BytesIO()
    imagen.save(salida, 'JPEG', quality=85, optimize=True)
    return salida.getvalue(), 'image/jpeg'


def _validar_hoja_vida(form, foto):
    """Devuelve ``(columnas, accesorios, equipo, error)`` listos para db.guardar_hoja_vida.

    Solo se aceptan las columnas de la lista fija; los ids se comprueban contra la base.
    """
    def texto(campo, maximo):
        valor = (form.get(campo) or '').replace('\x00', '').strip()
        if len(valor) > maximo:
            raise ValueError(f"El campo «{campo}» admite máximo {maximo} caracteres.")
        return valor or None

    def id_valido(campo, validos, nombre):
        valor = (form.get(campo) or '').strip()
        if not valor:
            return None
        if not valor.isdigit() or int(valor) not in validos:
            raise ValueError(f"{nombre} no válido.")
        return int(valor)

    try:
        equipos = {e['id']: e for e in db.inventario_sin_hoja_vida()}
        id_inventario = id_valido('id_inventario', equipos, 'Equipo')
        if id_inventario is None:
            raise ValueError("Selecciona el equipo (N° de serie).")

        datos = {
            'id_inventario': id_inventario,
            'id_responsable': id_valido('id_responsable', {r['id'] for r in db.responsables()},
                                        'Responsable'),
        }
        for campo, maximo in _HV_TEXTO.items():
            datos[campo] = texto(campo, maximo)
        for campo, opciones in _HV_OPCIONES.items():
            datos[campo] = texto(campo, 20)
            if datos[campo] is not None and datos[campo] not in opciones:
                raise ValueError(f"Valor no permitido en «{campo}».")

        fecha = texto('fecha_adquisicion', 10)
        if fecha:
            try:
                if not _FECHA.match(fecha):
                    raise ValueError
                datetime.strptime(fecha, '%Y-%m-%d')
            except ValueError:
                raise ValueError("La fecha de adquisición no es válida.")
        datos['fecha_adquisicion'] = fecha

        meses = form.getlist('meses_mantenimiento')
        if len(meses) > 12 or not all(m.isdigit() and 1 <= int(m) <= 12 for m in meses):
            raise ValueError("Los meses de mantenimiento no son válidos.")
        datos['meses_mantenimiento'] = sorted({int(m) for m in meses})

        marcas = {m[0] for m in db.marcas() or []}
        accesorios = []
        for n in range(1, _HV_FILAS_ACCESORIOS + 1):
            cantidad = texto(f'acc_cantidad_{n}', 6)
            fila = {
                'fila': n,
                'descripcion': texto(f'acc_descripcion_{n}', 255),
                'id_marca': id_valido(f'acc_marca_{n}', marcas, f'Marca del accesorio {n}'),
                'modelo': texto(f'acc_modelo_{n}', 120),
            }
            if cantidad is not None and not cantidad.isdigit():
                raise ValueError(f"La cantidad del accesorio {n} debe ser un número entero.")
            fila['cantidad'] = int(cantidad) if cantidad else None
            if any(fila[k] is not None for k in ('cantidad', 'descripcion', 'id_marca', 'modelo')):
                accesorios.append(fila)

        if foto and foto.filename:
            datos['foto'], datos['foto_mime'] = _procesar_foto(foto)
            datos['foto_nombre'] = os.path.basename(foto.filename.replace('\\', '/'))[:255] or None
    except ValueError as error:
        return None, None, None, str(error)

    return datos, accesorios, equipos[id_inventario], None


@sisem.route('/mantenimiento/hojas_vida_equipos/guardar', methods=['POST'])
@login_required
def guardar_hoja_vida():
    datos, accesorios, equipo, problema = _validar_hoja_vida(request.form, request.files.get('foto'))
    if problema:
        return jsonify({"error": problema}), 400

    try:
        hoja_id = db.guardar_hoja_vida(datos, accesorios)
    except db.HojaVidaDuplicada:
        return jsonify({"error": "Este equipo ya tiene una hoja de vida registrada."}), 409
    except Exception:
        logger.exception("No se pudo guardar la hoja de vida")
        return jsonify({"error": "No se pudo guardar la hoja de vida. Inténtalo de nuevo."}), 500

    return jsonify({
        "mensaje": "Hoja de vida guardada correctamente",
        "id": hoja_id,
        "archivo": informes.nombre_archivo_hoja_vida(equipo['inventario'], hoja_id),
    }), 201


@sisem.route('/mantenimiento/hojas_vida_equipos/<int:hoja_id>/descargar')
@login_required
def descargar_hoja_vida(hoja_id):
    try:
        pdf, nombre = informes.generar_hoja_vida_pdf(hoja_id)
    except informes.ReporteNoEncontrado:
        abort(404)
    except Exception:
        logger.exception("Error generando el PDF de la hoja de vida %s", hoja_id)
        return _error(500, "No se pudo generar el PDF. Inténtalo de nuevo más tarde.")

    return send_file(io.BytesIO(pdf), mimetype='application/pdf', as_attachment=True,
                    download_name=nombre)


@sisem.route('/consultar_hv')
@login_required
def consultar_hv():
    texto = request.args.get('q', '').replace('\x00', '').strip()[:120]
    pagina = max(1, request.args.get('pagina', 1, type=int))
    total = db.contar_hojas_vida(texto) or 0
    filas = db.buscar_hojas_vida(texto, POR_PAGINA, (pagina - 1) * POR_PAGINA) if total else []

    hojas = [dict(f, creada=informes.fecha_legible(f['fecha_creacion']),
                  archivo=informes.nombre_archivo_hoja_vida(f['numero_inventario'], f['id']))
             for f in filas]
    paginas = max(1, -(-total // POR_PAGINA))
    return render_template('forms/hv_equipos/consultar_hoja_vida.html', hojas=hojas, q=texto,
                           total=total, pagina=pagina, paginas=paginas)


# --------------------------------------------INVENTARIO--------------------------------------------
@sisem.route('/comercial/inventario')
@login_required
def inventario():
    return render_template('forms/comercial/inventario.html', inventario=db.inventario() or [])

def _formulario_inventario(error=None, valores=None, guardado=None, codigo=200):
    return render_template(
        'forms/comercial/ingresar_inventario.html',
        marcas=db.marcas() or [],
        tipo_equipos=db.tipo_equipos() or [],
        clientes=db.clientes() or [],
        riesgos=db.riesgo_equipos() or [],
        error=error, valores=valores or {}, guardado=guardado,
    ), codigo


@sisem.route('/comercial/ingresar_inventario')
@login_required
@solo_admin
def ingresar_inventario():
    return _formulario_inventario(guardado=request.args.get('guardado', '')[:120] or None)


def _catalogos_inventario():
    return {
        'tipos': db.tipo_equipos() or [],
        'marcas': db.marcas() or [],
        'clientes': db.clientes() or [],
        'riesgos': db.riesgo_equipos() or [],
    }


def _validar_inventario(form, catalogos=None):
    """Devuelve ``(columnas, error)``: {columna de inventario: valor} solo con campos conocidos.

    El token CSRF y cualquier campo extra del formulario se ignoran; los ids se comprueban
    contra los catálogos de la base (``catalogos`` permite reutilizarlos en la carga por Excel).
    """
    if catalogos is None:
        catalogos = _catalogos_inventario()

    def texto(campo, maximo, nombre):
        valor = (form.get(campo) or '').replace('\x00', '').strip()
        if not valor:
            raise ValueError(f"Falta {nombre}.")
        if len(valor) > maximo:
            raise ValueError(f"{nombre[0].upper() + nombre[1:]} admite máximo {maximo} caracteres.")
        return valor

    def id_de(campo, catalogo, nombre):
        valor = (form.get(campo) or '').strip()
        if not valor.isdigit() or int(valor) not in {fila[0] for fila in catalogo or []}:
            raise ValueError(f"Selecciona {nombre}.")
        return int(valor)

    try:
        riesgo = (form.get('riesgo') or '').strip()
        if riesgo not in {fila[1] for fila in catalogos['riesgos']}:
            raise ValueError("Selecciona la clasificación del riesgo.")
        return {
            'id_equipo': id_de('equipo', catalogos['tipos'], 'el tipo de equipo'),
            'marca': id_de('marca', catalogos['marcas'], 'la marca'),
            'lote': texto('lote', 120, 'el lote'),
            'serie': texto('serie', 120, 'la serie'),
            'serial_inventario_interno': texto('interno', 120, 'el serial de inventario interno'),
            'invima': texto('invima', 100, 'el registro INVIMA'),
            'id_cliente': id_de('cliente', catalogos['clientes'], 'el cliente'),
            'riesgo': riesgo,
        }, None
    except ValueError as error:
        return None, str(error)


_INVENTARIO_REPETIDO = {
    'serie': "Ya existe un equipo con esa serie.",
}


@sisem.route('/comercial/guardar_inventario', methods=['POST'])
@login_required
@solo_admin
def guardar_inventario():
    datos, problema = _validar_inventario(request.form)
    if problema:
        return _formulario_inventario(error=problema, valores=request.form, codigo=400)

    try:
        db.guardar_inventario(datos)
    except db.InventarioDuplicado as duplicado:
        mensaje = _INVENTARIO_REPETIDO.get(
            duplicado.campo, "Ya existe un equipo con esos datos (valor repetido).")
        return _formulario_inventario(error=mensaje, valores=request.form, codigo=409)
    except Exception:
        logger.exception("No se pudo guardar el inventario")
        return _formulario_inventario(error="No se pudo guardar el equipo. Inténtalo de nuevo.",
                                      valores=request.form, codigo=500)

    # Post/Redirect/Get: recargar la página no vuelve a enviar el formulario.
    return redirect(url_for('ingresar_inventario', guardado=datos['serial_inventario_interno']))

# ----------------------------------------------------------------------------------------
# Carga de inventario desde Excel (.xlsx)
# ----------------------------------------------------------------------------------------
# Encabezado (sin tildes ni mayúsculas) -> campo del formulario manual de inventario.
_EXCEL_COLUMNAS = {
    'tipo de equipo': 'equipo', 'equipo': 'equipo', 'marca': 'marca', 'serie': 'serie',
    'lote': 'lote', 'serial de inventario interno': 'interno', 'serial interno': 'interno',
    'cliente': 'cliente', 'riesgo': 'riesgo', 'clasificacion del riesgo': 'riesgo',
    'invima': 'invima', 'registro invima': 'invima',
}
_EXCEL_ENCABEZADOS = {   # orden y nombres que se muestran en la página
    'equipo': 'Tipo de equipo', 'marca': 'Marca', 'serie': 'Serie', 'lote': 'Lote',
    'interno': 'Serial de inventario interno', 'cliente': 'Cliente', 'riesgo': 'Riesgo',
    'invima': 'INVIMA',
}
_EXCEL_MAX_FILAS = 1000                        # filas con datos por archivo
_EXCEL_MAX_DESCOMPRIMIDO = 20 * 1024 * 1024    # un .xlsx es un ZIP: tope al descomprimir
_EXCEL_MAX_ENTRADAS_ZIP = 300
_EXCEL_MAX_COLUMNAS = 30
_EXCEL_ERRORES_VISIBLES = 50
# Un texto que empieza así se ejecutaría como fórmula si los datos se exportan a Excel
_EXCEL_PREFIJOS_FORMULA = ('=', '+', '@', '\t', '\r')


class _ExcelInvalido(Exception):
    """Problema con el archivo completo (formato, tamaño, encabezados). El mensaje es para el usuario."""


def _normalizar(texto):
    """'  Clínica  Norte ' -> 'clinica norte' (sin tildes, minúsculas, espacios simples)."""
    sin_tildes = ''.join(c for c in unicodedata.normalize('NFD', str(texto))
                         if unicodedata.category(c) != 'Mn')
    return ' '.join(sin_tildes.lower().split())


def _celda_texto(valor):
    """Valor de una celda como texto: 12345.0 -> '12345'; vacío -> ''."""
    if valor is None:
        return ''
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return str(valor).replace('\x00', '').strip()


def _abrir_excel(archivo):
    """Comprueba que sea un .xlsx real y razonable antes de dárselo a openpyxl.

    * Firma ZIP (un .xlsx es un ZIP): rechaza archivos renombrados.
    * Tamaño y número de entradas al descomprimir: evita "bombas ZIP" (archivos de pocos KB
      que ocupan gigas en memoria).
    * Sin macros (vbaProject).
    openpyxl usa defusedxml (requirements.txt), que bloquea los ataques XML (entidades externas
    y expansión de entidades). Solo modo lectura, sin fórmulas (valores calculados) ni vínculos.
    """
    contenido = archivo.read()          # máximo 1 MB por MAX_CONTENT_LENGTH
    if not contenido.startswith(b'PK\x03\x04'):
        raise _ExcelInvalido("El archivo no es un Excel .xlsx válido.")
    try:
        with zipfile.ZipFile(io.BytesIO(contenido)) as comprimido:
            entradas = comprimido.infolist()
            if (len(entradas) > _EXCEL_MAX_ENTRADAS_ZIP
                    or sum(e.file_size for e in entradas) > _EXCEL_MAX_DESCOMPRIMIDO):
                raise _ExcelInvalido("El archivo es demasiado grande. Divídelo en archivos más pequeños.")
            if any(e.filename.lower().startswith('xl/vbaproject') for e in entradas):
                raise _ExcelInvalido("El archivo contiene macros. Guárdalo como .xlsx sin macros.")
        return openpyxl.load_workbook(io.BytesIO(contenido), read_only=True, data_only=True,
                                      keep_links=False)
    except _ExcelInvalido:
        raise
    except Exception:
        logger.warning("Excel de inventario rechazado: no se pudo abrir", exc_info=True)
        raise _ExcelInvalido("El archivo no es un Excel .xlsx válido.")


def _leer_filas_excel(libro):
    """[(número de fila, {campo: texto})] de la primera hoja, usando la fila 1 como encabezado."""
    try:
        filas = libro.worksheets[0].iter_rows(values_only=True, max_col=_EXCEL_MAX_COLUMNAS)
        encabezado = next(filas, None) or ()
        columnas = {}
        for posicion, titulo in enumerate(encabezado):
            campo = _EXCEL_COLUMNAS.get(_normalizar(_celda_texto(titulo)))
            if campo and campo not in columnas.values():
                columnas[posicion] = campo
        faltan = [nombre for campo, nombre in _EXCEL_ENCABEZADOS.items() if campo not in columnas.values()]
        if faltan:
            raise _ExcelInvalido("Faltan estas columnas en la fila 1: " + ', '.join(faltan) + '.')

        resultado = []
        for numero, fila in enumerate(filas, start=2):
            valores = {campo: _celda_texto(fila[i]) if i < len(fila) else ''
                       for i, campo in columnas.items()}
            if not any(valores.values()):
                continue                                   # fila vacía
            if len(resultado) >= _EXCEL_MAX_FILAS:
                raise _ExcelInvalido(f"El archivo supera el máximo de {_EXCEL_MAX_FILAS} filas.")
            resultado.append((numero, valores))
    except _ExcelInvalido:
        raise
    except Exception:
        logger.warning("Excel de inventario rechazado: no se pudo leer", exc_info=True)
        raise _ExcelInvalido("No se pudo leer el archivo. Verifica que sea un Excel .xlsx válido.")
    if not resultado:
        raise _ExcelInvalido("El archivo no tiene filas con datos debajo del encabezado.")
    return resultado


def _indice_por_nombre(catalogo):
    """{nombre normalizado: id}. Si el catálogo repite un nombre, se usa el id menor
    (en tipo_equipo son los ids 1-16 que usan los formularios y PDF de mantenimiento)."""
    indice = {}
    for id_, nombre in catalogo:
        clave = _normalizar(nombre)
        indice[clave] = min(id_, indice.get(clave, id_))
    return indice


def _fila_excel_a_formulario(valores, indices):
    """Convierte los nombres de la fila en los ids/valores que espera _validar_inventario."""
    for campo, valor in valores.items():
        if valor.startswith(_EXCEL_PREFIJOS_FORMULA):
            raise ValueError(f"«{_EXCEL_ENCABEZADOS[campo]}» no puede empezar por =, +, @ (parece una fórmula).")

    form = dict(valores)
    for campo, indice in indices.items():
        if not valores[campo]:
            continue                                       # _validar_inventario dirá qué falta
        clave = _normalizar(valores[campo])
        nombre = _EXCEL_ENCABEZADOS[campo]
        if clave not in indice:
            raise ValueError(f"{nombre} «{valores[campo][:60]}» no existe en el sistema.")
        form[campo] = str(indice[clave])
    return form


def _pagina_excel(error=None, resultado=None, codigo=200):
    return render_template('forms/comercial/cargar_excel.html', error=error, resultado=resultado,
                           columnas=list(_EXCEL_ENCABEZADOS.values()), max_filas=_EXCEL_MAX_FILAS), codigo


@sisem.route('/comercial/cargar_excel')
@login_required
@solo_admin
def cargar_excel():
    return _pagina_excel()


@sisem.route('/comercial/procesar_excel', methods=['POST'])
@login_required
@solo_admin
def procesar_excel():
    """Valida y guarda cada fila por separado: una fila con error no detiene las demás."""
    archivo = request.files.get('archivo_excel')
    if not archivo or not archivo.filename:
        return _pagina_excel(error="Selecciona un archivo de Excel (.xlsx).", codigo=400)
    if not archivo.filename.lower().endswith('.xlsx'):
        return _pagina_excel(error="El archivo debe ser de Excel en formato .xlsx.", codigo=400)

    try:
        libro = _abrir_excel(archivo)
        try:
            filas = _leer_filas_excel(libro)
        finally:
            libro.close()
    except _ExcelInvalido as error:
        return _pagina_excel(error=str(error), codigo=400)

    catalogos = _catalogos_inventario()
    indices = {
        'equipo': _indice_por_nombre(catalogos['tipos']),
        'marca': _indice_por_nombre(catalogos['marcas']),
        'cliente': _indice_por_nombre(catalogos['clientes']),
    }
    riesgos = {_normalizar(nombre): nombre for _, nombre in catalogos['riesgos']}

    guardadas, errores = 0, []
    for numero, valores in filas:
        try:
            form = _fila_excel_a_formulario(valores, indices)
            form['riesgo'] = riesgos.get(_normalizar(valores['riesgo']), valores['riesgo'])
            datos, problema = _validar_inventario(form, catalogos)
            if problema:
                raise ValueError(problema)
            db.guardar_inventario(datos)
            guardadas += 1
        except ValueError as error:
            errores.append((numero, str(error)))
        except db.InventarioDuplicado as duplicado:      # la serie ya existe (único UNIQUE de inventario)
            errores.append((numero, _INVENTARIO_REPETIDO.get(duplicado.campo, "Valor repetido.")))
        except Exception:
            logger.exception("No se pudo guardar la fila %s del Excel de inventario", numero)
            errores.append((numero, "No se pudo guardar. Inténtalo de nuevo."))

    logger.info("Carga de inventario por Excel: %s filas guardadas, %s con error", guardadas, len(errores))
    return _pagina_excel(resultado={
        'guardadas': guardadas,
        'total_errores': len(errores),
        'errores': errores[:_EXCEL_ERRORES_VISIBLES],
    })


# ----------------------------------------------------------------------------------------
# Cronograma de mantenimientos
# ----------------------------------------------------------------------------------------
ESTADOS_CRONOGRAMA = {
    'pendiente': 'Pendiente', 'en_proceso': 'En proceso',
    'realizado': 'Realizado', 'cancelado': 'Cancelado',
}
FRECUENCIAS_CRONOGRAMA = {
    0: 'Una sola vez', 1: 'Mensual', 2: 'Bimestral', 3: 'Trimestral',
    4: 'Cuatrimestral', 6: 'Semestral', 12: 'Anual',
}
_MESES = ('Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto',
          'Septiembre', 'Octubre', 'Noviembre', 'Diciembre')
_DIAS_SEMANA = ('Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom')
_HORIZONTE_MESES = 12             # la frecuencia genera las fechas de los próximos 12 meses
_MAX_OBSERVACIONES = 1000


def _sumar_meses(fecha, meses):
    """31/01 + 1 mes -> 28/02: si el mes no tiene ese día, se usa el último."""
    total = fecha.month - 1 + meses
    anio, mes = fecha.year + total // 12, total % 12 + 1
    return date(anio, mes, min(fecha.day, calendar.monthrange(anio, mes)[1]))


def _leer_fecha(texto):
    """date o None; acepta solo AAAA-MM-DD real y entre 2000 y 2100."""
    texto = (texto or '').strip()
    if not _FECHA.match(texto):
        return None
    try:
        fecha = datetime.strptime(texto, '%Y-%m-%d').date()
    except ValueError:
        return None
    return fecha if 2000 <= fecha.year <= 2100 else None


def _arg_entero(nombre, minimo, maximo):
    """Parámetro entero de la URL dentro de un rango, o None si falta o no es válido."""
    valor = request.args.get(nombre, type=int)
    return valor if valor is not None and minimo <= valor <= maximo else None


def _nombres_sin_repetir(catalogo):
    """Catálogo [(id, nombre)] sin nombres repetidos (se conserva el id menor)."""
    unicos = {}
    for id_, nombre in sorted(catalogo or []):
        unicos.setdefault(_normalizar(nombre), (id_, nombre))
    return sorted(unicos.values(), key=lambda fila: fila[1])


def _decorar_cronograma(fila, hoy):
    """Agrega lo que muestran las plantillas. 'Vencido' no se guarda: es un pendiente con fecha pasada."""
    vencido = fila['estado'] == 'pendiente' and fila['fecha_programada'] < hoy
    fila['estado_clase'] = 'vencido' if vencido else fila['estado']
    fila['estado_nombre'] = 'Vencido' if vencido else ESTADOS_CRONOGRAMA.get(fila['estado'], fila['estado'])
    fila['fecha_texto'] = fila['fecha_programada'].strftime('%d/%m/%Y')
    return fila


@sisem.route('/mantenimiento/planificado')
@login_required
def cronograma():
    """Cronograma: vista de mes (calendario + agenda) o de año (equipos × 12 meses)."""
    hoy = date.today()
    vista = 'anio' if request.args.get('vista') == 'anio' else 'mes'
    anio = _arg_entero('anio', 2000, 2100) or hoy.year
    mes = _arg_entero('mes', 1, 12) or hoy.month
    estado = request.args.get('estado', '')
    if estado != 'vencido' and estado not in ESTADOS_CRONOGRAMA:
        estado = ''
    id_tecnico = _arg_entero('tecnico', 1, 2147483647)
    id_cliente = _arg_entero('cliente', 1, 2147483647)

    if vista == 'mes':
        desde, hasta = date(anio, mes, 1), date(anio, mes, calendar.monthrange(anio, mes)[1])
    else:
        desde, hasta = date(anio, 1, 1), date(anio, 12, 31)
    filas = [_decorar_cronograma(f, hoy)
             for f in db.cronograma_entre(desde, hasta, hoy, estado or None, id_tecnico, id_cliente)]

    contexto = {}
    if vista == 'mes':
        por_dia = defaultdict(list)
        for fila in filas:
            por_dia[fila['fecha_programada']].append(fila)
        anterior, siguiente = _sumar_meses(desde, -1), _sumar_meses(desde, 1)
        contexto.update(
            semanas=calendar.Calendar().monthdatescalendar(anio, mes),
            por_dia=por_dia, titulo_periodo=f"{_MESES[mes - 1]} {anio}",
            anterior={'anio': anterior.year, 'mes': anterior.month},
            siguiente={'anio': siguiente.year, 'mes': siguiente.month},
        )
    else:
        equipos = {}
        for fila in filas:
            equipo = equipos.setdefault(fila['id_equipo'], {
                'inventario': fila['inventario'], 'serie': fila['serie'], 'equipo': fila['equipo'],
                'cliente': fila['cliente'], 'meses': defaultdict(list),
            })
            equipo['meses'][fila['fecha_programada'].month].append(fila)
        contexto.update(
            matriz=sorted(equipos.values(), key=lambda e: (e['cliente'] or '', e['inventario'] or '')),
            titulo_periodo=str(anio),
            anterior={'anio': anio - 1, 'mes': mes}, siguiente={'anio': anio + 1, 'mes': mes},
        )

    filtros = {k: v for k, v in (('estado', estado), ('tecnico', id_tecnico), ('cliente', id_cliente)) if v}
    equipos_disponibles = db.equipos_cronograma() or []
    clientes = sorted({(e['id_cliente'], e['cliente']) for e in equipos_disponibles if e['cliente']},
                      key=lambda fila: fila[1])
    return render_template(
        'forms/cronograma/cronograma.html',
        vista=vista, anio=anio, mes=mes, hoy=hoy, filas=filas, filtros=filtros,
        estado=estado, id_tecnico=id_tecnico, id_cliente=id_cliente,
        resumen={
            'total': len(filas),
            'pendientes': sum(f['estado_clase'] == 'pendiente' for f in filas),
            'vencidos': sum(f['estado_clase'] == 'vencido' for f in filas),
            'realizados': sum(f['estado_clase'] == 'realizado' for f in filas),
        },
        estados=ESTADOS_CRONOGRAMA, meses=_MESES, dias_semana=_DIAS_SEMANA,
        tecnicos=db.personal() or [], clientes=clientes,
        aviso=request.args.get('aviso'), creados=_arg_entero('creados', 0, 1000),
        omitidos=_arg_entero('omitidos', 0, 1000),
        **contexto,
    )


def _pagina_programar(error=None, valores=None, codigo=200):
    return render_template(
        'forms/cronograma/programar.html',
        equipos=db.equipos_cronograma() or [],
        tipos=_nombres_sin_repetir(db.servicios()),
        tecnicos=db.personal() or [],
        frecuencias=FRECUENCIAS_CRONOGRAMA, horizonte=_HORIZONTE_MESES,
        error=error, valores=valores or {},
    ), codigo


@sisem.route('/mantenimiento/planificado/nuevo')
@login_required
def programar_mantenimiento():
    fecha = _leer_fecha(request.args.get('fecha'))     # viene al pulsar un día del calendario
    return _pagina_programar(valores={'fecha_programada': fecha.isoformat()} if fecha else None)


def _validar_programacion(form):
    """Devuelve ``(filas, error)``: una fila por cada fecha que genera la frecuencia."""
    def id_en(campo, validos):
        valor = (form.get(campo) or '').strip()
        return int(valor) if valor.isdigit() and int(valor) in validos else None

    id_equipo = id_en('id_equipo', {e['id'] for e in db.equipos_cronograma() or []})
    if id_equipo is None:
        return None, "Selecciona el equipo."
    id_tipo = id_en('tipo_mantenimiento', {f[0] for f in db.servicios() or []})
    if id_tipo is None:
        return None, "Selecciona el tipo de mantenimiento."
    fecha = _leer_fecha(form.get('fecha_programada'))
    if fecha is None:
        return None, "La fecha programada no es válida."
    frecuencia = (form.get('frecuencia_meses') or '').strip()
    if not frecuencia.isdigit() or int(frecuencia) not in FRECUENCIAS_CRONOGRAMA:
        return None, "Selecciona la frecuencia."
    frecuencia = int(frecuencia)
    id_tecnico = None
    if (form.get('tecnico_asignado_id') or '').strip():
        id_tecnico = id_en('tecnico_asignado_id', {f[0] for f in db.personal() or []})
        if id_tecnico is None:
            return None, "El técnico seleccionado no es válido."
    observaciones = (form.get('observaciones') or '').replace('\x00', '').strip()
    if len(observaciones) > _MAX_OBSERVACIONES:
        return None, f"Las observaciones admiten máximo {_MAX_OBSERVACIONES} caracteres."

    # Frecuencia 3 -> meses 0, 3, 6, 9 desde la fecha inicial; "una sola vez" -> solo esa fecha.
    saltos = range(0, _HORIZONTE_MESES, frecuencia) if frecuencia else [0]
    return [{
        'id_equipo': id_equipo, 'tipo_mantenimiento': id_tipo, 'frecuencia_meses': frecuencia or None,
        'fecha_programada': _sumar_meses(fecha, salto), 'tecnico_asignado_id': id_tecnico,
        'observaciones': observaciones or None,
    } for salto in saltos], None


@sisem.route('/mantenimiento/planificado/guardar', methods=['POST'])
@login_required
def guardar_programacion():
    filas, problema = _validar_programacion(request.form)
    if problema:
        return _pagina_programar(error=problema, valores=request.form, codigo=400)
    try:
        creados, omitidos = db.programar_mantenimientos(filas)
    except Exception:
        logger.exception("No se pudo guardar la programación del cronograma")
        return _pagina_programar(error="No se pudo guardar la programación. Inténtalo de nuevo.",
                                 valores=request.form, codigo=500)
    inicio = filas[0]['fecha_programada']
    return redirect(url_for('cronograma', anio=inicio.year, mes=inicio.month, aviso='creado',
                            creados=creados, omitidos=omitidos))


def _pagina_detalle_cronograma(fila, error=None, codigo=200):
    return render_template('forms/cronograma/detalle.html', m=_decorar_cronograma(fila, date.today()),
                           estados=ESTADOS_CRONOGRAMA, frecuencias=FRECUENCIAS_CRONOGRAMA,
                           tecnicos=db.personal() or [], error=error), codigo


@sisem.route('/mantenimiento/planificado/<int:cronograma_id>')
@login_required
def detalle_cronograma(cronograma_id):
    fila = db.obtener_cronograma(cronograma_id)
    if not fila:
        abort(404)
    return _pagina_detalle_cronograma(fila)


@sisem.route('/mantenimiento/planificado/<int:cronograma_id>/actualizar', methods=['POST'])
@login_required
def actualizar_cronograma(cronograma_id):
    fila = db.obtener_cronograma(cronograma_id)
    if not fila:
        abort(404)

    form = request.form
    estado = form.get('estado', '')
    fecha = _leer_fecha(form.get('fecha_programada'))
    observaciones = (form.get('observaciones') or '').replace('\x00', '').strip()
    texto_tecnico = (form.get('tecnico_asignado_id') or '').strip()
    id_tecnico = int(texto_tecnico) if texto_tecnico.isdigit() else None

    problema = None
    if estado not in ESTADOS_CRONOGRAMA:
        problema = "Selecciona un estado válido."
    elif fecha is None:
        problema = "La fecha programada no es válida."
    elif texto_tecnico and id_tecnico not in {f[0] for f in db.personal() or []}:
        problema = "El técnico seleccionado no es válido."
    elif len(observaciones) > _MAX_OBSERVACIONES:
        problema = f"Las observaciones admiten máximo {_MAX_OBSERVACIONES} caracteres."
    if problema:
        return _pagina_detalle_cronograma(fila, error=problema, codigo=400)

    try:
        db.actualizar_cronograma(cronograma_id, estado, fecha, id_tecnico, observaciones or None)
    except Exception:
        logger.exception("No se pudo actualizar el cronograma %s", cronograma_id)
        return _pagina_detalle_cronograma(fila, error="No se pudo guardar. Inténtalo de nuevo.", codigo=500)
    return redirect(url_for('cronograma', anio=fecha.year, mes=fecha.month, aviso='actualizado'))

# -------------------------------------------REPORTE GENERAL---------------------------------------------
_RG_FILAS_REPUESTOS = 5
# Campo de texto del formulario -> (etiqueta para los mensajes, longitud máxima, obligatorio)
_RG_TEXTO = {
    'solicitante': ('Solicitante', 50, True),
    'ciudad': ('Ciudad', 50, True),
    'departamento': ('Departamento', 50, True),
    'direccion': ('Dirección', 120, True),
    'modelo': ('Modelo', 50, True),
    'inventario': ('N° de inventario', 50, True),
    'descripcion': ('Descripción de la falla o solicitud', 2000, True),
    'diagnostico': ('Diagnóstico técnico', 2000, True),
    'actividad': ('Actividad realizada', 2000, True),
    'observaciones': ('Observaciones', 2000, False),
    'estado_final': ('Estado final del equipo', 50, True),
    'recibido_por': ('Recibido por', 50, True),
}


def _validar_reporte_general(form):
    """Devuelve ``(datos, error)``; ``datos`` está listo para db.guardar_reporte_general.

    Solo se leen los campos conocidos y cada id se comprueba contra su catálogo en la base.
    """
    def elegido(campo, catalogo, etiqueta):
        """(id, nombre) del catálogo [(id, nombre)] que eligió el usuario."""
        valor = (form.get(campo) or '').strip()
        if not valor:
            raise ValueError(f"Selecciona «{etiqueta}».")
        nombres = dict(catalogo or [])
        if not valor.isdigit() or int(valor) not in nombres:
            raise ValueError(f"«{etiqueta}» no es válido.")
        return int(valor), nombres[int(valor)]

    def fecha(campo, etiqueta):
        valor = _leer_fecha(form.get(campo))
        if valor is None:
            raise ValueError(f"«{etiqueta}» no es una fecha válida.")
        return valor

    try:
        texto = {}
        for campo, (etiqueta, maximo, obligatorio) in _RG_TEXTO.items():
            valor = (form.get(campo) or '').replace('\x00', '').strip()
            if len(valor) > maximo:
                raise ValueError(f"«{etiqueta}» admite máximo {maximo} caracteres.")
            if obligatorio and not valor:
                raise ValueError(f"Completa el campo «{etiqueta}».")
            texto[campo] = valor or None

        fecha_reporte = fecha('fecha', 'Fecha')
        fecha_entrega = fecha('fecha_entrega', 'Fecha de entrega')
        if fecha_entrega < fecha_reporte:
            raise ValueError("La fecha de entrega no puede ser anterior a la fecha del reporte.")

        id_cliente, _ = elegido('cliente', db.clientes(), 'Cliente')
        _, area = elegido('area', db.areas(), 'Área')
        id_serie, serie = elegido('serie', [(e['id'], e['serie']) for e in db.inventario() or []],
                                  'N° de serie')
        id_tipo, tipo = elegido('tipo_equipo', db.tipo_equipos(), 'Tipo de equipo')
        id_marca, marca = elegido('marca', db.marcas(), 'Marca')
        id_servicio, _ = elegido('servicio', db.servicios(), 'Tipo de servicio')
        id_tecnico, _ = elegido('entregado_por', db.personal(), 'Entregado por')

        cantidades = form.getlist('repuesto_cantidad')
        descripciones = form.getlist('repuesto_descripcion')
        if len(cantidades) > _RG_FILAS_REPUESTOS or len(descripciones) > _RG_FILAS_REPUESTOS:
            raise ValueError("Hay demasiadas filas de repuestos.")
        repuestos = []
        for n, (cantidad, descripcion) in enumerate(zip(cantidades, descripciones), start=1):
            cantidad = cantidad.strip()
            descripcion = descripcion.replace('\x00', '').strip()
            if not cantidad and not descripcion:
                continue
            if not cantidad.isdigit() or not 1 <= int(cantidad) <= 9999:
                raise ValueError(f"La cantidad del repuesto {n} debe ser un número entre 1 y 9999.")
            if not descripcion or len(descripcion) > 120:
                raise ValueError(f"Escribe la descripción del repuesto {n} (máximo 120 caracteres).")
            repuestos.append({'cantidad': int(cantidad), 'descripcion': descripcion})
    except ValueError as error:
        return None, str(error)

    # Lo que no tiene columna propia va en «informacion» (jsonb). Se guardan id y nombre
    # para que el reporte siga mostrando el texto aunque el catálogo cambie después.
    informacion = {
        'fecha': fecha_reporte.isoformat(),
        'departamento': texto['departamento'],
        'direccion': texto['direccion'],
        'equipo': {
            'serie': serie,
            'tipo_equipo': {'id': id_tipo, 'nombre': tipo},
            'marca': {'id': id_marca, 'nombre': marca},
            'modelo': texto['modelo'],
            'inventario': texto['inventario'],
        },
        'descripcion': texto['descripcion'],
        'diagnostico': texto['diagnostico'],
        'actividad': texto['actividad'],
        'repuestos': repuestos,
        'observaciones': texto['observaciones'],
        'estado_final': texto['estado_final'],
        'recibido_por': texto['recibido_por'],
    }
    return {
        'cliente': id_cliente,
        'area': area[:50],                 # la columna es varchar(50) y guarda el nombre
        'solicitante': texto['solicitante'],
        'ciudad': texto['ciudad'],
        'serie': id_serie,                 # id del equipo en inventario (llave foránea)
        'servicio': id_servicio,
        'informacion': informacion,
        'fecha_entrega': fecha_entrega,
        'entregado_por': id_tecnico,
    }, None


def _pagina_reporte_general(valores=None, error=None, guardado=None, codigo=200):
    return render_template(
        'forms/reporte_general/crear_reporte_general.html',
        clientes=db.clientes() or [],
        areas=db.areas() or [],
        equipos=db.inventario() or [],
        tipos=db.tipo_equipos() or [],
        marcas=db.marcas() or [],
        servicios=db.servicios() or [],
        personal=db.personal() or [],
        filas_repuestos=_RG_FILAS_REPUESTOS,
        v=valores if valores is not None else MultiDict(),
        error=error, guardado=guardado,
    ), codigo


@sisem.route('/mantenimiento/reporte_general')
@login_required
def crear_reporte_general():
    return _pagina_reporte_general(guardado=_arg_entero('guardado', 1, 2**31 - 1))


@sisem.route('/mantenimiento/reporte_general/guardar', methods=['POST'])
@login_required
def guardar_reporte_general():
    datos, problema = _validar_reporte_general(request.form)
    if problema:
        return _pagina_reporte_general(request.form, error=problema, codigo=400)

    try:
        reporte_id = db.guardar_reporte_general(datos)
    except Exception:
        logger.exception("No se pudo guardar el reporte general")
        return _pagina_reporte_general(request.form, codigo=500,
                                       error="No se pudo guardar el reporte. Inténtalo de nuevo.")
    # Post/Redirect/Get: recargar la página no vuelve a enviar el formulario
    return redirect(url_for('crear_reporte_general', guardado=reporte_id))

@sisem.route('/mantenimiento/reporte_general/consultar_reportes')
@login_required
def consultar_reportes_generales():
    # La consulta ya trae los nombres (JOIN en la base): no hace falta enviar los catálogos
    return render_template('forms/reporte_general/consultar_reportes_generales.html',
                           reportes=db.consultar_reporte_general(),
                           nombre_pdf=informes.nombre_archivo_reporte_general)


@sisem.route('/mantenimiento/reporte_general/<int:reporte_id>/descargar')
@login_required
def descargar_reporte_general(reporte_id):
    """PDF del reporte general (formato FT-GC-041) armado con los datos guardados en la base."""
    try:
        pdf, nombre = informes.generar_reporte_general_pdf(reporte_id)
    except informes.ReporteNoEncontrado:
        abort(404)
    except Exception:
        logger.exception("Error generando el PDF del reporte general %s", reporte_id)
        return _error(500, "No se pudo generar el PDF. Inténtalo de nuevo más tarde.")

    return send_file(io.BytesIO(pdf), mimetype='application/pdf', as_attachment=True,
                     download_name=nombre)


# =========================================================================================
# Utilidades comunes de los módulos de Comercial, Talento humano, Laboratorio y Mi cuenta
# =========================================================================================
_DINERO = re.compile(r'^\d{1,12}(\.\d{1,2})?$')
_CENTAVOS = Decimal('0.01')
_TOPE_DINERO = Decimal('999999999999.99')            # numeric(14,2)


class _Formulario:
    """Lee y valida campos de un formulario. Cada método lanza ValueError con un mensaje para
    el usuario; solo se leen los campos que pide el código (cualquier otro se ignora)."""

    def __init__(self, form):
        self.form = form

    def bruto(self, campo):
        return (self.form.get(campo) or '').replace('\x00', '').strip()

    def texto(self, campo, etiqueta, maximo, obligatorio=True):
        valor = self.bruto(campo)
        if obligatorio and not valor:
            raise ValueError(f"Completa el campo «{etiqueta}».")
        if len(valor) > maximo:
            raise ValueError(f"«{etiqueta}» admite máximo {maximo} caracteres.")
        return valor or None

    def opcion(self, campo, etiqueta, opciones):
        valor = self.bruto(campo)
        if valor not in opciones:
            raise ValueError(f"Selecciona un valor válido en «{etiqueta}».")
        return valor

    def id_de(self, campo, etiqueta, validos, obligatorio=True):
        valor = self.bruto(campo)
        if not valor:
            if obligatorio:
                raise ValueError(f"Selecciona «{etiqueta}».")
            return None
        if not valor.isdigit() or int(valor) not in validos:
            raise ValueError(f"«{etiqueta}» no es válido.")
        return int(valor)

    def fecha(self, campo, etiqueta, obligatorio=True):
        bruto = self.bruto(campo)
        if not bruto and not obligatorio:
            return None
        valor = _leer_fecha(bruto)
        if valor is None:
            raise ValueError(f"«{etiqueta}» no es una fecha válida.")
        return valor

    def dinero(self, campo, etiqueta, obligatorio=True, minimo=Decimal('0')):
        bruto = self.bruto(campo).replace(',', '.')
        if not bruto:
            if obligatorio:
                raise ValueError(f"Completa el campo «{etiqueta}».")
            return Decimal('0')
        if not _DINERO.match(bruto) or not minimo <= Decimal(bruto) <= _TOPE_DINERO:
            raise ValueError(f"«{etiqueta}» debe ser un valor válido (sin puntos de miles, máximo 2 decimales).")
        return Decimal(bruto)

    def entero(self, campo, etiqueta, minimo, maximo):
        bruto = self.bruto(campo)
        if not bruto.isdigit() or not minimo <= int(bruto) <= maximo:
            raise ValueError(f"«{etiqueta}» debe ser un número entre {minimo} y {maximo}.")
        return int(bruto)


sisem.jinja_env.filters['pesos'] = lambda valor: informes.pesos(valor) or '—'
sisem.jinja_env.filters['fecha'] = informes.fecha_legible
sisem.jinja_env.globals['numero_documento'] = informes.numero_documento


def _enviar_pdf(generar, registro, descripcion):
    """Genera el PDF con ``generar(registro)`` -> (bytes, nombre) y lo envía como descarga."""
    try:
        pdf, nombre = generar(registro)
    except Exception:
        logger.exception("Error generando el PDF de %s", descripcion)
        return _error(500, "No se pudo generar el PDF. Inténtalo de nuevo más tarde.")
    return send_file(io.BytesIO(pdf), mimetype='application/pdf', as_attachment=True,
                     download_name=nombre)


def _ids(catalogo):
    return {fila[0] for fila in catalogo or []}


def _estado_visible(estados, estado, fecha=None):
    """(texto, clase CSS) del estado; un pendiente con fecha pasada se muestra 'Vencido'."""
    if estado == 'pendiente' and fecha and fecha < date.today():
        return 'Vencido', 'vencido'
    return estados.get(estado, (estado or '—', 'cancelado'))


sisem.jinja_env.globals['estado_visible'] = _estado_visible


# Rutas antiguas del menú que ahora tienen su propia página
_RUTAS_MOVIDAS = {
    ('comercial', 'clientes'): 'facturacion',
    ('talento_humano', 'asistencia'): 'cuentas_cobro',
    ('talento_humano', 'reportes'): 'nuevo_empleado',
    ('laboratorio', 'hojas_de_vida'): 'consultar_hv',
}


@sisem.route('/laboratorio/hojas_de_vida')
@sisem.route('/talento_humano/reportes')
@sisem.route('/talento_humano/asistencia')
@sisem.route('/comercial/clientes')
@login_required
def ruta_movida():
    partes = tuple(request.path.strip('/').split('/'))
    return redirect(url_for(_RUTAS_MOVIDAS[partes]))


# =========================================================================================
# Comercial: facturación (a clientes) y órdenes de compra (a proveedores) · solo admin
# Ambas son "documentos con ítems": misma validación de ítems, totales y plantillas.
# =========================================================================================
ESTADOS_FACTURA = {'pendiente': ('Pendiente', 'pendiente'), 'pagada': ('Pagada', 'realizado'),
                   'anulada': ('Anulada', 'cancelado')}
ESTADOS_ORDEN = {'pendiente': ('Pendiente', 'pendiente'), 'aprobada': ('Aprobada', 'en_proceso'),
                 'recibida': ('Recibida', 'realizado'), 'anulada': ('Anulada', 'cancelado')}
_FILAS_ITEMS = 10
_IVA_PERMITIDO = ('0', '5', '19')                     # tarifas de IVA vigentes en Colombia
_TOPE_CANTIDAD = Decimal('1000000')

# Configuración de cada tipo de documento (textos y endpoints de sus rutas)
_DOC = {
    'factura': {'tipo': 'factura', 'titulo': 'Factura', 'plural': 'Facturación', 'prefijo': 'FAC',
                'estados': ESTADOS_FACTURA, 'listado': 'facturacion', 'nuevo': 'nueva_factura',
                'guardar': 'guardar_factura', 'detalle': 'detalle_factura', 'estado': 'estado_factura',
                'pdf': 'descargar_factura', 'id': 'factura_id'},
    'orden': {'tipo': 'orden', 'titulo': 'Orden de compra', 'plural': 'Órdenes de compra', 'prefijo': 'OC',
              'estados': ESTADOS_ORDEN, 'listado': 'ordenes', 'nuevo': 'nueva_orden',
              'guardar': 'guardar_orden', 'detalle': 'detalle_orden', 'estado': 'estado_orden',
              'pdf': 'descargar_orden', 'id': 'orden_id'},
}


def _leer_items(form):
    """Ítems del formulario (filas vacías se ignoran). Lanza ValueError."""
    columnas = [form.getlist(c) for c in ('item_descripcion', 'item_cantidad', 'item_valor', 'item_iva')]
    if any(len(c) > _FILAS_ITEMS for c in columnas):
        raise ValueError("Hay demasiadas filas de ítems.")
    items = []
    for n, fila in enumerate(zip_longest(*columnas, fillvalue=''), start=1):
        descripcion, cantidad, valor, iva = (x.replace('\x00', '').strip() for x in fila)
        if not (descripcion or cantidad or valor):
            continue
        cantidad, valor = cantidad.replace(',', '.'), valor.replace(',', '.')
        if not descripcion or len(descripcion) > 255:
            raise ValueError(f"Escribe la descripción del ítem {n} (máximo 255 caracteres).")
        if not _DINERO.match(cantidad) or not Decimal('0') < Decimal(cantidad) <= _TOPE_CANTIDAD:
            raise ValueError(f"La cantidad del ítem {n} debe ser mayor que 0 (máximo {_TOPE_CANTIDAD:,}).")
        if not _DINERO.match(valor) or Decimal(valor) > _TOPE_DINERO:
            raise ValueError(f"El valor unitario del ítem {n} no es válido (sin puntos de miles).")
        if (iva or '0') not in _IVA_PERMITIDO:
            raise ValueError(f"El IVA del ítem {n} no es válido.")
        items.append({'fila': len(items) + 1, 'descripcion': descripcion, 'cantidad': Decimal(cantidad),
                      'valor_unitario': Decimal(valor), 'iva_pct': Decimal(iva or '0')})
    if not items:
        raise ValueError("Agrega al menos un ítem con descripción, cantidad y valor.")
    return items


def _totales(items):
    """(subtotal, iva, total) redondeados a centavos. El servidor siempre los recalcula."""
    subtotal = sum((i['cantidad'] * i['valor_unitario'] for i in items), Decimal('0'))
    iva = sum((i['cantidad'] * i['valor_unitario'] * i['iva_pct'] / 100 for i in items), Decimal('0'))
    subtotal, iva = (x.quantize(_CENTAVOS, ROUND_HALF_UP) for x in (subtotal, iva))
    if subtotal + iva > _TOPE_DINERO:
        raise ValueError("El total del documento es demasiado grande.")
    return subtotal, iva, subtotal + iva


def _validar_documento(tipo, form):
    """(datos, items, error) para db.guardar_documento."""
    f = _Formulario(form)
    try:
        if tipo == 'factura':
            emision = f.fecha('fecha_emision', 'Fecha de emisión')
            vence = f.fecha('fecha_vencimiento', 'Fecha de vencimiento')
            if vence < emision:
                raise ValueError("La fecha de vencimiento no puede ser anterior a la de emisión.")
            datos = {'id_cliente': f.id_de('cliente', 'Cliente', _ids(db.clientes())),
                     'fecha_emision': emision, 'fecha_vencimiento': vence}
        else:
            fecha = f.fecha('fecha', 'Fecha de la orden')
            entrega = f.fecha('fecha_entrega', 'Fecha de entrega', obligatorio=False)
            if entrega and entrega < fecha:
                raise ValueError("La fecha de entrega no puede ser anterior a la fecha de la orden.")
            datos = {'proveedor': f.texto('proveedor', 'Proveedor', 150),
                     'nit_proveedor': f.texto('nit_proveedor', 'NIT del proveedor', 30, obligatorio=False),
                     'contacto': f.texto('contacto', 'Contacto', 150, obligatorio=False),
                     'fecha': fecha, 'fecha_entrega': entrega}
        items = _leer_items(form)
        subtotal, iva, total = _totales(items)
        datos.update(observaciones=f.texto('observaciones', 'Observaciones', 2000, obligatorio=False),
                     subtotal=subtotal, iva=iva, total=total, creado_por=session.get('username'))
    except ValueError as error:
        return None, None, str(error)
    return datos, items, None


def _pagina_documento(tipo, valores=None, error=None, codigo=200):
    return render_template('forms/comercial/documento_form.html', doc=_DOC[tipo],
                           clientes=db.clientes() or [], filas=_FILAS_ITEMS, ivas=_IVA_PERMITIDO,
                           v=valores if valores is not None else MultiDict(), error=error,
                           hoy=date.today().isoformat()), codigo


def _guardar_documento(tipo):
    datos, items, problema = _validar_documento(tipo, request.form)
    if problema:
        return _pagina_documento(tipo, request.form, problema, 400)
    try:
        nuevo_id = db.guardar_documento(tipo, datos, items)
    except Exception:
        logger.exception("No se pudo guardar el documento %s", tipo)
        return _pagina_documento(tipo, request.form, "No se pudo guardar. Inténtalo de nuevo.", 500)
    return redirect(url_for(_DOC[tipo]['detalle'], **{_DOC[tipo]['id']: nuevo_id}, aviso='creado'))


def _detalle_documento(tipo, documento):
    if not documento:
        abort(404)
    return render_template('forms/comercial/documento_detalle.html', doc=_DOC[tipo], d=documento,
                           numero=informes.numero_documento(_DOC[tipo]['prefijo'], documento['id']),
                           aviso=request.args.get('aviso'))


def _cambiar_estado_documento(tipo, documento_id):
    estado = request.form.get('estado', '')
    if estado not in _DOC[tipo]['estados']:
        return _error(400, "Estado no válido.")
    if not db.actualizar_estado_documento(tipo, documento_id, estado):
        abort(404)
    return redirect(url_for(_DOC[tipo]['detalle'], **{_DOC[tipo]['id']: documento_id}, aviso='estado'))


@sisem.route('/comercial/facturacion')
@login_required
@solo_admin
def facturacion():
    return render_template('forms/comercial/facturacion.html', facturas=db.listar_facturas(),
                           estados=ESTADOS_FACTURA)


@sisem.route('/comercial/facturacion/nueva')
@login_required
@solo_admin
def nueva_factura():
    return _pagina_documento('factura')


@sisem.route('/comercial/facturacion/guardar', methods=['POST'])
@login_required
@solo_admin
def guardar_factura():
    return _guardar_documento('factura')


@sisem.route('/comercial/facturacion/<int:factura_id>')
@login_required
@solo_admin
def detalle_factura(factura_id):
    return _detalle_documento('factura', db.obtener_factura(factura_id))


@sisem.route('/comercial/facturacion/<int:factura_id>/estado', methods=['POST'])
@login_required
@solo_admin
def estado_factura(factura_id):
    return _cambiar_estado_documento('factura', factura_id)


@sisem.route('/comercial/facturacion/<int:factura_id>/descargar')
@login_required
@solo_admin
def descargar_factura(factura_id):
    factura = db.obtener_factura(factura_id) or abort(404)
    return _enviar_pdf(informes.generar_factura_pdf, factura, f"la factura {factura_id}")


@sisem.route('/comercial/ordenes')
@login_required
@solo_admin
def ordenes():
    return render_template('forms/comercial/ordenes.html', ordenes=db.listar_ordenes(),
                           estados=ESTADOS_ORDEN)


@sisem.route('/comercial/ordenes/nueva')
@login_required
@solo_admin
def nueva_orden():
    return _pagina_documento('orden')


@sisem.route('/comercial/ordenes/guardar', methods=['POST'])
@login_required
@solo_admin
def guardar_orden():
    return _guardar_documento('orden')


@sisem.route('/comercial/ordenes/<int:orden_id>')
@login_required
@solo_admin
def detalle_orden(orden_id):
    return _detalle_documento('orden', db.obtener_orden(orden_id))


@sisem.route('/comercial/ordenes/<int:orden_id>/estado', methods=['POST'])
@login_required
@solo_admin
def estado_orden(orden_id):
    return _cambiar_estado_documento('orden', orden_id)


@sisem.route('/comercial/ordenes/<int:orden_id>/descargar')
@login_required
@solo_admin
def descargar_orden(orden_id):
    orden = db.obtener_orden(orden_id) or abort(404)
    return _enviar_pdf(informes.generar_orden_pdf, orden, f"la orden de compra {orden_id}")


# =========================================================================================
# Talento humano: empleados y roles/permisos (solo admin)
# =========================================================================================
_DOCUMENTO_ID = re.compile(r'^[0-9A-Za-z.\-]{4,20}$')
_CORREO = re.compile(r'^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$')
_TELEFONO = re.compile(r'^[0-9+()\s\-]{7,20}$')
_USUARIO = re.compile(r'^[A-Za-z0-9._\-]{3,60}$')
_CLAVE_MIN, _CLAVE_MAX = 10, 128

# Lo que puede hacer cada rol. Se muestra en Roles y permisos; quien lo hace cumplir son los
# decoradores @solo_admin y los filtros por personal_id de cada ruta.
PERMISOS = [
    ('Inicio, clientes y mi cuenta', 'Ver', 'Ver y cambiar su contraseña'),
    ('Mantenimiento: reportes, historial, reporte general, hojas de vida y cronograma', 'Todo', 'Todo'),
    ('Comercial · Inventario', 'Ver, ingresar equipos y cargar Excel', 'Solo ver'),
    ('Comercial · Facturación y órdenes de compra', 'Todo', 'Sin acceso'),
    ('Talento humano · Empleados, roles y permisos', 'Todo', 'Sin acceso'),
    ('Talento humano · Cuentas de cobro', 'Ver todas, radicar y cambiar estado', 'Radicar y ver las propias'),
    ('Talento humano · Nómina', 'Liquidar y ver todas', 'Ver y descargar sus desprendibles'),
    ('Laboratorio · Metrología y hojas de vida', 'Todo', 'Todo'),
]


def _validar_clave(nueva, confirmar, usuario):
    """Mensaje de error o None. Reglas: 10-128 caracteres, igual a la confirmación, distinta del usuario."""
    if not _CLAVE_MIN <= len(nueva) <= _CLAVE_MAX:
        return f"La contraseña debe tener entre {_CLAVE_MIN} y {_CLAVE_MAX} caracteres."
    if nueva != confirmar:
        return "La contraseña y su confirmación no coinciden."
    if usuario and nueva.lower() == usuario.lower():
        return "La contraseña no puede ser igual al usuario."
    return None


def _validar_empleado(form):
    f = _Formulario(form)
    try:
        datos = {
            'nombre': f.texto('nombre', 'Nombre completo', 120),
            'documento': f.texto('documento', 'Documento', 20),
            'cargo': f.texto('cargo', 'Cargo', 80),
            'profesion': f.texto('profesion', 'Profesión', 80),
            'correo': f.texto('correo', 'Correo', 120, obligatorio=False) or '',
            'telefono': f.texto('telefono', 'Teléfono', 20, obligatorio=False),
        }
        if not _DOCUMENTO_ID.match(datos['documento']):
            raise ValueError("El documento solo admite números, letras, puntos y guiones (4 a 20).")
        if datos['correo'] and not _CORREO.match(datos['correo']):
            raise ValueError("El correo no es válido.")
        if datos['telefono'] and not _TELEFONO.match(datos['telefono']):
            raise ValueError("El teléfono no es válido.")
    except ValueError as error:
        return None, str(error)
    return datos, None


def _pagina_empleado(valores=None, empleado_id=None, error=None, codigo=200):
    return render_template('forms/talento_humano/empleado_form.html', v=valores or {},
                           empleado_id=empleado_id, error=error), codigo


@sisem.route('/talento_humano/empleados')
@login_required
@solo_admin
def empleados():
    return render_template('forms/talento_humano/empleados.html', empleados=db.empleados(),
                           guardado=request.args.get('guardado'))


@sisem.route('/talento_humano/empleados/nuevo')
@login_required
@solo_admin
def nuevo_empleado():
    return _pagina_empleado()


@sisem.route('/talento_humano/empleados/<int:empleado_id>/editar')
@login_required
@solo_admin
def editar_empleado(empleado_id):
    empleado = db.obtener_empleado(empleado_id) or abort(404)
    return _pagina_empleado(empleado, empleado_id)


@sisem.route('/talento_humano/empleados/guardar', methods=['POST'])
@login_required
@solo_admin
def guardar_empleado():
    bruto_id = (request.form.get('id') or '').strip()
    empleado_id = int(bruto_id) if bruto_id.isdigit() else None
    datos, problema = _validar_empleado(request.form)
    if problema:
        return _pagina_empleado(request.form, empleado_id, problema, 400)
    try:
        guardado = db.guardar_empleado(datos, empleado_id)
    except Exception:
        logger.exception("No se pudo guardar el empleado %s", empleado_id)
        return _pagina_empleado(request.form, empleado_id, "No se pudo guardar. Inténtalo de nuevo.", 500)
    if guardado is None:
        abort(404)
    return redirect(url_for('empleados', guardado=datos['nombre']))


@sisem.route('/talento_humano/roles')
@login_required
@solo_admin
def roles():
    return render_template('forms/talento_humano/roles.html', empleados=db.empleados(),
                           permisos=PERMISOS, roles=ROLES, admin_usuario=ADMIN_USER,
                           guardado=request.args.get('guardado'))


def _pagina_acceso(empleado, error=None, codigo=200, valores=None):
    return render_template('forms/talento_humano/acceso.html', e=empleado, error=error,
                           v=valores or {}, clave_min=_CLAVE_MIN), codigo


@sisem.route('/talento_humano/roles/<int:empleado_id>')
@login_required
@solo_admin
def configurar_acceso(empleado_id):
    return _pagina_acceso(db.obtener_empleado(empleado_id) or abort(404))


@sisem.route('/talento_humano/roles/<int:empleado_id>/guardar', methods=['POST'])
@login_required
@solo_admin
def guardar_acceso(empleado_id):
    empleado = db.obtener_empleado(empleado_id) or abort(404)
    usuario = (request.form.get('usuario') or '').strip()
    nueva = request.form.get('clave', '')
    activo = request.form.get('acceso_activo') == 'si'

    problema = None
    if not _USUARIO.match(usuario):
        problema = "El usuario debe tener de 3 a 60 caracteres: letras, números, punto, guion o guion bajo."
    elif ADMIN_USER and usuario.lower() == ADMIN_USER.lower():
        problema = "Ese usuario está reservado para el administrador."
    elif nueva:
        problema = _validar_clave(nueva, request.form.get('confirmar', ''), usuario)
    elif activo and not empleado.get('tiene_clave'):
        problema = "Asigna una contraseña para poder activar el acceso."
    if problema:
        return _pagina_acceso(empleado, problema, 400, {'usuario': usuario})

    try:
        cambiado = db.configurar_acceso(empleado_id, usuario,
                                        generate_password_hash(nueva) if nueva else None, activo)
    except db.UsuarioDuplicado:
        return _pagina_acceso(empleado, "Ese usuario ya lo tiene otra persona.", 409, {'usuario': usuario})
    except Exception:
        logger.exception("No se pudo configurar el acceso del empleado %s", empleado_id)
        return _pagina_acceso(empleado, "No se pudo guardar. Inténtalo de nuevo.", 500, {'usuario': usuario})
    if not cambiado:
        abort(404)
    logger.info("Acceso del empleado %s actualizado por %s (activo=%s, clave nueva=%s)",
                empleado_id, session.get('username'), activo, bool(nueva))
    return redirect(url_for('roles', guardado=empleado['nombre']))


# =========================================================================================
# Talento humano: cuentas de cobro (el funcionario radica y ve las suyas; el admin gestiona)
# =========================================================================================
ESTADOS_CUENTA = {'pendiente': ('Pendiente', 'pendiente'), 'aprobada': ('Aprobada', 'en_proceso'),
                  'pagada': ('Pagada', 'realizado'), 'rechazada': ('Rechazada', 'vencido')}


def _registro_propio(registro):
    """El registro si es visible para la sesión (admin: todos; funcionario: solo los suyos).
    Un registro ajeno responde 404 (no se revela que existe)."""
    if not registro or (not es_admin() and registro.get('id_personal') != session.get('personal_id')):
        abort(404)
    return registro


def _pagina_cuenta(valores=None, error=None, codigo=200):
    return render_template('forms/talento_humano/cuenta_cobro_form.html',
                           empleados=(db.personal() or []) if es_admin() else [],
                           v=valores if valores is not None else MultiDict(), error=error,
                           hoy=date.today().isoformat()), codigo


@sisem.route('/talento_humano/cuentas_cobro')
@login_required
def cuentas_cobro():
    filas = db.listar_cuentas_cobro(None if es_admin() else session.get('personal_id'))
    return render_template('forms/talento_humano/cuentas_cobro.html', cuentas=filas, estados=ESTADOS_CUENTA)


@sisem.route('/talento_humano/cuentas_cobro/nueva')
@login_required
def nueva_cuenta_cobro():
    return _pagina_cuenta()


@sisem.route('/talento_humano/cuentas_cobro/guardar', methods=['POST'])
@login_required
def guardar_cuenta_cobro():
    f = _Formulario(request.form)
    try:
        # El funcionario solo radica a su nombre: el campo "empleado" del formulario se ignora.
        id_personal = (f.id_de('empleado', 'Empleado', _ids(db.personal())) if es_admin()
                       else session.get('personal_id'))
        inicio = f.fecha('periodo_inicio', 'Inicio del periodo')
        fin = f.fecha('periodo_fin', 'Fin del periodo')
        if fin < inicio:
            raise ValueError("El fin del periodo no puede ser anterior al inicio.")
        datos = {'id_personal': id_personal, 'fecha': f.fecha('fecha', 'Fecha'),
                 'periodo_inicio': inicio, 'periodo_fin': fin,
                 'concepto': f.texto('concepto', 'Concepto', 500),
                 'valor': f.dinero('valor', 'Valor', minimo=Decimal('0.01')),
                 'observaciones': f.texto('observaciones', 'Observaciones', 1000, obligatorio=False),
                 'creado_por': session.get('username')}
    except ValueError as error:
        return _pagina_cuenta(request.form, str(error), 400)
    try:
        nuevo_id = db.guardar_cuenta_cobro(datos)
    except Exception:
        logger.exception("No se pudo guardar la cuenta de cobro")
        return _pagina_cuenta(request.form, "No se pudo guardar. Inténtalo de nuevo.", 500)
    return redirect(url_for('detalle_cuenta_cobro', cuenta_id=nuevo_id, aviso='creado'))


@sisem.route('/talento_humano/cuentas_cobro/<int:cuenta_id>')
@login_required
def detalle_cuenta_cobro(cuenta_id):
    cuenta = _registro_propio(db.obtener_cuenta_cobro(cuenta_id))
    return render_template('forms/talento_humano/cuenta_cobro_detalle.html', c=cuenta,
                           estados=ESTADOS_CUENTA, aviso=request.args.get('aviso'))


@sisem.route('/talento_humano/cuentas_cobro/<int:cuenta_id>/estado', methods=['POST'])
@login_required
@solo_admin
def estado_cuenta_cobro(cuenta_id):
    f = _Formulario(request.form)
    try:
        estado = f.opcion('estado', 'Estado', ESTADOS_CUENTA)
        observaciones = f.texto('observaciones', 'Observaciones', 1000, obligatorio=False)
    except ValueError as error:
        return _error(400, str(error))
    if not db.actualizar_estado_cuenta(cuenta_id, estado, observaciones):
        abort(404)
    return redirect(url_for('detalle_cuenta_cobro', cuenta_id=cuenta_id, aviso='estado'))


@sisem.route('/talento_humano/cuentas_cobro/<int:cuenta_id>/descargar')
@login_required
def descargar_cuenta_cobro(cuenta_id):
    cuenta = _registro_propio(db.obtener_cuenta_cobro(cuenta_id))
    return _enviar_pdf(informes.generar_cuenta_cobro_pdf, cuenta, f"la cuenta de cobro {cuenta_id}")


# =========================================================================================
# Talento humano: nómina (el admin liquida; el funcionario ve sus desprendibles)
# Cálculo simplificado: salud 4 % y pensión 4 % sobre (salario proporcional + horas extra).
# =========================================================================================
APORTE_SALUD = Decimal('0.04')
APORTE_PENSION = Decimal('0.04')
_PESO = Decimal('1')
_PERIODO = re.compile(r'^(\d{4})-(\d{2})$')


def _leer_periodo(texto):
    """'2026-11' -> date(2026, 11, 1); None si no es válido."""
    coincide = _PERIODO.match((texto or '').strip())
    if not coincide:
        return None
    anio, mes = int(coincide.group(1)), int(coincide.group(2))
    return date(anio, mes, 1) if 2000 <= anio <= 2100 and 1 <= mes <= 12 else None


def liquidar_nomina(salario, dias, auxilio, extras, bonificaciones, otras):
    """Totales de la liquidación, redondeados al peso. Función pura (probada en los tests)."""
    def peso(x):
        return x.quantize(_PESO, ROUND_HALF_UP)
    salario_periodo = peso(salario * dias / 30)
    auxilio_periodo = peso(auxilio * dias / 30)
    base_aportes = salario_periodo + extras
    salud, pension = peso(base_aportes * APORTE_SALUD), peso(base_aportes * APORTE_PENSION)
    devengado = salario_periodo + auxilio_periodo + extras + bonificaciones
    deducciones = salud + pension + otras
    return {'salario_periodo': salario_periodo, 'auxilio_periodo': auxilio_periodo, 'salud': salud,
            'pension': pension, 'total_devengado': devengado, 'total_deducciones': deducciones,
            'neto_pagar': devengado - deducciones}


def _pagina_nomina(valores=None, error=None, codigo=200):
    return render_template('forms/talento_humano/nomina_form.html', empleados=db.personal() or [],
                           v=valores if valores is not None else MultiDict(), error=error,
                           mes_actual=date.today().strftime('%Y-%m'),
                           salud=int(APORTE_SALUD * 100), pension=int(APORTE_PENSION * 100)), codigo


@sisem.route('/talento_humano/nomina')
@login_required
def nomina():
    filas = db.listar_nomina(None if es_admin() else session.get('personal_id'))
    return render_template('forms/talento_humano/nomina.html', nominas=filas)


@sisem.route('/talento_humano/nomina/nueva')
@login_required
@solo_admin
def nueva_nomina():
    return _pagina_nomina()


@sisem.route('/talento_humano/nomina/guardar', methods=['POST'])
@login_required
@solo_admin
def guardar_nomina():
    f = _Formulario(request.form)
    try:
        periodo = _leer_periodo(request.form.get('periodo'))
        if periodo is None:
            raise ValueError("«Periodo» no es válido (mes y año).")
        datos = {'id_personal': f.id_de('empleado', 'Empleado', _ids(db.personal())), 'periodo': periodo,
                 'salario_base': f.dinero('salario_base', 'Salario base'),
                 'dias_trabajados': f.entero('dias_trabajados', 'Días trabajados', 0, 30),
                 'auxilio_transporte': f.dinero('auxilio_transporte', 'Auxilio de transporte', obligatorio=False),
                 'horas_extra': f.dinero('horas_extra', 'Horas extra y recargos', obligatorio=False),
                 'bonificaciones': f.dinero('bonificaciones', 'Bonificaciones', obligatorio=False),
                 'otras_deducciones': f.dinero('otras_deducciones', 'Otras deducciones', obligatorio=False),
                 'observaciones': f.texto('observaciones', 'Observaciones', 1000, obligatorio=False),
                 'creado_por': session.get('username')}
        calculo = liquidar_nomina(datos['salario_base'], datos['dias_trabajados'], datos['auxilio_transporte'],
                                  datos['horas_extra'], datos['bonificaciones'], datos['otras_deducciones'])
        if calculo['neto_pagar'] < 0:
            raise ValueError("Las deducciones superan lo devengado: revisa los valores.")
        datos.update({k: calculo[k] for k in ('salud', 'pension', 'total_devengado',
                                              'total_deducciones', 'neto_pagar')})
    except ValueError as error:
        return _pagina_nomina(request.form, str(error), 400)
    try:
        nuevo_id = db.guardar_nomina(datos)
    except db.NominaDuplicada:
        return _pagina_nomina(request.form, "Ese empleado ya tiene liquidada la nómina de ese mes.", 409)
    except Exception:
        logger.exception("No se pudo guardar la nómina")
        return _pagina_nomina(request.form, "No se pudo guardar. Inténtalo de nuevo.", 500)
    return redirect(url_for('detalle_nomina', nomina_id=nuevo_id, aviso='creado'))


@sisem.route('/talento_humano/nomina/<int:nomina_id>')
@login_required
def detalle_nomina(nomina_id):
    registro = _registro_propio(db.obtener_nomina(nomina_id))
    return render_template('forms/talento_humano/nomina_detalle.html', n=registro,
                           aviso=request.args.get('aviso'))


@sisem.route('/talento_humano/nomina/<int:nomina_id>/descargar')
@login_required
def descargar_nomina(nomina_id):
    registro = _registro_propio(db.obtener_nomina(nomina_id))
    return _enviar_pdf(informes.generar_nomina_pdf, registro, f"la nómina {nomina_id}")


# =========================================================================================
# Laboratorio: cronograma y reportes de metrología (admin y funcionario)
# =========================================================================================
TIPOS_METROLOGIA = informes.TIPOS_METROLOGIA
ESTADOS_METROLOGIA = {'pendiente': ('Pendiente', 'pendiente'), 'en_proceso': ('En proceso', 'en_proceso'),
                      'realizado': ('Realizado', 'realizado'), 'cancelado': ('Cancelado', 'cancelado')}
_FILAS_MEDICION = 10
_MEDIDA = re.compile(r'^-?\d{1,9}(\.\d{1,6})?$')


def _validar_programacion_metrologia(form, actualizar=False):
    f = _Formulario(form)
    try:
        datos = {
            'fecha_programada': f.fecha('fecha_programada', 'Fecha programada'),
            'proveedor': f.texto('proveedor', 'Laboratorio / proveedor', 150, obligatorio=False),
            'id_responsable': f.id_de('responsable', 'Responsable', _ids(db.personal()), obligatorio=False),
            'observaciones': f.texto('observaciones', 'Observaciones', 1000, obligatorio=False),
        }
        if actualizar:
            datos['estado'] = f.opcion('estado', 'Estado', ESTADOS_METROLOGIA)
        else:
            datos['id_equipo'] = f.id_de('equipo', 'Equipo', {e['id'] for e in db.equipos_cronograma() or []})
            datos['tipo'] = f.opcion('tipo', 'Tipo de actividad', TIPOS_METROLOGIA)
    except ValueError as error:
        return None, str(error)
    return datos, None


def _pagina_programar_metrologia(valores=None, error=None, codigo=200):
    return render_template('forms/laboratorio/metrologia_form.html', equipos=db.equipos_cronograma() or [],
                           personal=db.personal() or [], tipos=TIPOS_METROLOGIA,
                           v=valores if valores is not None else MultiDict(request.args), error=error), codigo


@sisem.route('/laboratorio/cronograma_metrologia')
@login_required
def cronograma_metrologia():
    hoy = date.today()
    anio = _arg_entero('anio', 2000, 2100) or hoy.year
    estado = request.args.get('estado') if request.args.get('estado') in ESTADOS_METROLOGIA else None
    try:
        filas = db.metrologia_entre(date(anio, 1, 1), date(anio, 12, 31), estado)
    except Exception:
        logger.exception("No se pudo consultar el cronograma de metrología")
        return _error(500, "No se pudo consultar el cronograma de metrología.")
    meses = defaultdict(list)
    for fila in filas:
        meses[fila['fecha_programada'].month].append(fila)
    resumen = {
        'total': len(filas),
        'pendientes': sum(f['estado'] == 'pendiente' for f in filas),
        'vencidos': sum(f['estado'] == 'pendiente' and f['fecha_programada'] < hoy for f in filas),
        'realizados': sum(f['estado'] == 'realizado' for f in filas),
    }
    return render_template('forms/laboratorio/cronograma_metrologia.html', anio=anio, estado=estado,
                           meses=sorted(meses.items()), resumen=resumen, tipos=TIPOS_METROLOGIA,
                           estados=ESTADOS_METROLOGIA, nombres_mes=_NOMBRES_MES,
                           aviso=request.args.get('aviso'))


_NOMBRES_MES = ('', 'Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto',
                'Septiembre', 'Octubre', 'Noviembre', 'Diciembre')


@sisem.route('/laboratorio/cronograma_metrologia/nuevo')
@login_required
def programar_metrologia():
    return _pagina_programar_metrologia()


@sisem.route('/laboratorio/cronograma_metrologia/guardar', methods=['POST'])
@login_required
def guardar_metrologia():
    datos, problema = _validar_programacion_metrologia(request.form)
    if problema:
        return _pagina_programar_metrologia(request.form, problema, 400)
    try:
        db.programar_metrologia(datos)
    except db.MetrologiaDuplicada:
        return _pagina_programar_metrologia(
            request.form, "Ese equipo ya tiene esa actividad programada en esa fecha.", 409)
    except Exception:
        logger.exception("No se pudo programar la actividad de metrología")
        return _pagina_programar_metrologia(request.form, "No se pudo guardar. Inténtalo de nuevo.", 500)
    return redirect(url_for('cronograma_metrologia', anio=datos['fecha_programada'].year, aviso='creado'))


def _pagina_detalle_metrologia(fila, error=None, codigo=200):
    return render_template('forms/laboratorio/metrologia_detalle.html', act=fila, personal=db.personal() or [],
                           tipos=TIPOS_METROLOGIA, estados=ESTADOS_METROLOGIA, error=error,
                           aviso=request.args.get('aviso')), codigo


@sisem.route('/laboratorio/cronograma_metrologia/<int:metrologia_id>')
@login_required
def detalle_metrologia(metrologia_id):
    return _pagina_detalle_metrologia(db.obtener_metrologia(metrologia_id) or abort(404))


@sisem.route('/laboratorio/cronograma_metrologia/<int:metrologia_id>/actualizar', methods=['POST'])
@login_required
def actualizar_metrologia(metrologia_id):
    fila = db.obtener_metrologia(metrologia_id) or abort(404)
    datos, problema = _validar_programacion_metrologia(request.form, actualizar=True)
    if problema:
        return _pagina_detalle_metrologia(fila, problema, 400)
    try:
        db.actualizar_metrologia(metrologia_id, datos)
    except Exception:
        logger.exception("No se pudo actualizar la metrología %s", metrologia_id)
        return _pagina_detalle_metrologia(fila, "No se pudo guardar. Inténtalo de nuevo.", 500)
    return redirect(url_for('detalle_metrologia', metrologia_id=metrologia_id, aviso='actualizado'))


def calcular_mediciones(filas):
    """[(patrón, medido, tolerancia) en texto] -> (mediciones, resultado). Función pura.

    error = medido − patrón; cumple si |error| ≤ tolerancia. El resultado es 'conforme' solo si
    todas las mediciones cumplen. Lanza ValueError si un valor no es numérico.
    """
    mediciones = []
    for n, (patron, medido, tolerancia) in enumerate(filas, start=1):
        valores = [x.replace(',', '.').strip() for x in (patron, medido, tolerancia)]
        if not any(valores):
            continue
        if not all(_MEDIDA.match(x) for x in valores) or Decimal(valores[2]) < 0:
            raise ValueError(f"La medición {n} debe tener valor patrón, valor medido y tolerancia "
                             "numéricos (tolerancia ≥ 0).")
        p, m, t = (Decimal(x) for x in valores)
        error = m - p
        mediciones.append({'n': len(mediciones) + 1, 'patron': str(p), 'medido': str(m),
                           'tolerancia': str(t), 'error': str(error), 'cumple': abs(error) <= t})
    if not mediciones:
        raise ValueError("Registra al menos una medición.")
    return mediciones, ('conforme' if all(x['cumple'] for x in mediciones) else 'no_conforme')


def _pagina_reporte_metrologia(valores=None, error=None, codigo=200):
    if valores is None:
        valores = MultiDict(request.args)
        if not es_admin():                                    # el técnico por defecto es quien entra
            valores.setdefault('tecnico', str(session.get('personal_id', '')))
        programada = db.obtener_metrologia(int(valores['programacion'])) \
            if valores.get('programacion', '').isdigit() else None
        if programada:                                        # viene desde el cronograma
            valores.setdefault('equipo', str(programada['id_equipo']))
            valores.setdefault('tipo', programada['tipo'])
    return render_template('forms/laboratorio/reporte_metrologia_form.html',
                           equipos=db.equipos_cronograma() or [], personal=db.personal() or [],
                           programadas=db.metrologia_por_realizar() or [], tipos=TIPOS_METROLOGIA,
                           filas=_FILAS_MEDICION, v=valores, error=error,
                           hoy=date.today().isoformat()), codigo


@sisem.route('/laboratorio/reportes_metrologia')
@login_required
def reportes_metrologia():
    return render_template('forms/laboratorio/reportes_metrologia.html',
                           reportes=db.listar_reportes_metrologia(), tipos=TIPOS_METROLOGIA,
                           guardado=_arg_entero('guardado', 1, 2**31 - 1))


@sisem.route('/laboratorio/reportes_metrologia/nuevo')
@login_required
def nuevo_reporte_metrologia():
    return _pagina_reporte_metrologia()


@sisem.route('/laboratorio/reportes_metrologia/guardar', methods=['POST'])
@login_required
def guardar_reporte_metrologia():
    form = request.form
    f = _Formulario(form)
    try:
        columnas = [form.getlist(c) for c in ('med_patron', 'med_medido', 'med_tolerancia')]
        if any(len(c) > _FILAS_MEDICION for c in columnas):
            raise ValueError("Hay demasiadas filas de mediciones.")
        mediciones, resultado = calcular_mediciones(zip_longest(*columnas, fillvalue=''))
        id_equipo = f.id_de('equipo', 'Equipo', {e['id'] for e in db.equipos_cronograma() or []})
        programadas = {p['id']: p for p in db.metrologia_por_realizar() or []}
        id_cronograma = f.id_de('programacion', 'Actividad programada', programadas, obligatorio=False)
        if id_cronograma and programadas[id_cronograma]['id_equipo'] != id_equipo:
            raise ValueError("La actividad programada elegida es de otro equipo.")
        fecha = f.fecha('fecha', 'Fecha')
        proxima = f.fecha('proxima_fecha', 'Próxima fecha', obligatorio=False)
        if proxima and proxima <= fecha:
            raise ValueError("La próxima fecha debe ser posterior a la fecha del reporte.")
        datos = {'id_equipo': id_equipo, 'id_cronograma': id_cronograma,
                 'tipo': f.opcion('tipo', 'Tipo de actividad', TIPOS_METROLOGIA), 'fecha': fecha,
                 'id_tecnico': f.id_de('tecnico', 'Técnico', _ids(db.personal())),
                 'patron': f.texto('patron', 'Equipo patrón', 150),
                 'certificado_patron': f.texto('certificado_patron', 'Certificado del patrón', 80,
                                               obligatorio=False),
                 'magnitud': f.texto('magnitud', 'Magnitud', 60), 'unidad': f.texto('unidad', 'Unidad', 20),
                 'mediciones': mediciones, 'resultado': resultado, 'proxima_fecha': proxima,
                 'observaciones': f.texto('observaciones', 'Observaciones', 2000, obligatorio=False),
                 'creado_por': session.get('username')}
    except ValueError as error:
        return _pagina_reporte_metrologia(form, str(error), 400)
    try:
        nuevo_id = db.guardar_reporte_metrologia(datos)
    except Exception:
        logger.exception("No se pudo guardar el reporte de metrología")
        return _pagina_reporte_metrologia(form, "No se pudo guardar. Inténtalo de nuevo.", 500)
    return redirect(url_for('reportes_metrologia', guardado=nuevo_id))


@sisem.route('/laboratorio/reportes_metrologia/<int:reporte_id>/descargar')
@login_required
def descargar_reporte_metrologia(reporte_id):
    reporte = db.obtener_reporte_metrologia(reporte_id) or abort(404)
    return _enviar_pdf(informes.generar_metrologia_pdf, reporte, f"el reporte de metrología {reporte_id}")


# =========================================================================================
# Mi cuenta: datos de quien inició sesión; el funcionario puede cambiar su contraseña
# =========================================================================================
@sisem.route('/cuenta')
@login_required
def mi_cuenta(error=None, codigo=200):
    empleado = None if es_admin() else db.obtener_empleado(session.get('personal_id'))
    return render_template('cuenta.html', empleado=empleado, error=error, clave_min=_CLAVE_MIN,
                           aviso=request.args.get('aviso')), codigo


@sisem.route('/cuenta/clave', methods=['POST'])
@login_required
@rol_requerido(ROL_FUNCIONARIO)
def cambiar_mi_clave():
    personal_id = session.get('personal_id')
    clave_intentos = f"clave:{personal_id}"
    if _intentos_recientes(clave_intentos) >= MAX_INTENTOS:
        return mi_cuenta("Demasiados intentos. Espera unos minutos.", 429)

    estado = db.estado_acceso(personal_id) or {}
    if not check_password_hash(estado.get('clave_hash') or _HASH_FALSO, request.form.get('actual', '')):
        _registrar_fallo(clave_intentos)
        return mi_cuenta("La contraseña actual no es correcta.", 400)
    nueva = request.form.get('nueva', '')
    problema = _validar_clave(nueva, request.form.get('confirmar', ''), session.get('username'))
    if problema:
        return mi_cuenta(problema, 400)

    nuevo_hash = generate_password_hash(nueva)
    try:
        db.cambiar_clave(personal_id, nuevo_hash)
    except Exception:
        logger.exception("No se pudo cambiar la clave del funcionario %s", personal_id)
        return mi_cuenta("No se pudo cambiar la contraseña. Inténtalo de nuevo.", 500)
    session['huella'] = _huella(nuevo_hash)       # esta sesión sigue; las demás se cierran
    _fallos.pop(clave_intentos, None)
    logger.info("El funcionario %s cambió su contraseña", personal_id)
    return redirect(url_for('mi_cuenta', aviso='clave'))


if __name__ == '__main__':
    sisem.run(debug=DEBUG, host=os.getenv('FLASK_HOST', '127.0.0.1'),
        port=int(os.getenv('FLASK_PORT', '5000')))

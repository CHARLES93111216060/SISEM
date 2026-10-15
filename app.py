"""SISEM - Sistema Integrado de Seguimiento de Equipos Médicos (Flask)."""
import hmac
import io
import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from functools import wraps

from dotenv import load_dotenv

load_dotenv()

from flask import (Flask, abort, jsonify, redirect, render_template, request,  # noqa: E402
                   send_file, session, url_for)
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
# Autenticación
# ----------------------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            if _quiere_json():
                return jsonify({"error": "not authenticated"}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


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

        usuario = request.form.get('username', '').strip()
        clave = request.form.get('password', '')
        usuario_ok = bool(ADMIN_USER) and hmac.compare_digest(usuario.encode(), ADMIN_USER.encode())
        clave_ok = check_password_hash(ADMIN_HASH or _HASH_FALSO, clave)   # siempre se evalúa

        if usuario_ok and clave_ok and ADMIN_HASH:
            session.clear()                 # evita fijación de sesión
            session['username'] = usuario
            session.permanent = True
            _fallos.pop(ip, None)
            logger.info("Inicio de sesión de %s", usuario)
            return redirect(url_for('dashboard'))

        _registrar_fallo(ip)
        logger.warning("Credenciales inválidas desde %s", ip)
        return render_template('login.html', error='Credenciales inválidas'), 401

    if 'username' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html')


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
    return render_template('dashboard.html', est=estadisticas)


@sisem.route('/clientes')
@login_required
def ver_clientes():
    return render_template('clientes.html', clientes=db.clientes() or [])


# Módulos del menú que aún no existen: muestran una página clara en lugar de un 404.
PROXIMAMENTE = {
    ('mantenimiento', 'planificado'): 'Cronograma de mantenimiento',
    ('mantenimiento', 'solicitudes'): 'Solicitudes de mantenimiento',
    ('comercial', 'clientes'): 'Facturación',
    ('comercial', 'ordenes'): 'Órdenes de compra',
    ('comercial', 'inventario'): 'Inventario',
    ('talento_humano', 'empleados'): 'Empleados',
    ('talento_humano', 'roles'): 'Roles y permisos',
    ('talento_humano', 'asistencia'): 'Cuentas de cobro',
    ('talento_humano', 'nomina'): 'Nómina',
    ('talento_humano', 'reportes'): 'Modificar lista de empleados',
    ('laboratorio', 'cronograma_metrologia'): 'Cronograma de metrología',
    ('laboratorio', 'reportes_metrologia'): 'Generar reportes de metrología',
    ('laboratorio', 'hojas_de_vida'): 'Consulta de hojas de vida',
}


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
    if form_type != 'Protocolo_de_mantenimiento':     # el único que usa los catálogos
        return render_template(plantilla)
    return render_template(
        plantilla,
        tecnicos=db.personal() or [],
        brand=db.marcas() or [],
        servicios=db.servicios() or [],
        clientes=db.clientes() or [],
        tipo_equipos=db.tipo_equipos() or [],
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
        limpios[clave] = valor

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


if __name__ == '__main__':
    sisem.run(debug=DEBUG, host=os.getenv('FLASK_HOST', '127.0.0.1'),
              port=int(os.getenv('FLASK_PORT', '5000')))

"""SISEM - Sistema Integrado de Seguimiento de Equipos Médicos (Flask)."""
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
from functools import wraps

import openpyxl
from dotenv import load_dotenv
from PIL import Image, ImageOps

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
def cargar_excel():
    return _pagina_excel()


@sisem.route('/comercial/procesar_excel', methods=['POST'])
@login_required
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


if __name__ == '__main__':
    sisem.run(debug=DEBUG, host=os.getenv('FLASK_HOST', '127.0.0.1'),
        port=int(os.getenv('FLASK_PORT', '5000')))

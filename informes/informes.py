"""Generación de informes PDF de SISEM.

Estrategia: plantillas HTML + CSS (Jinja2, carpeta ``templates/pdf/``) que se
convierten a PDF con WeasyPrint. No requiere Microsoft Word ni LibreOffice, así
que funciona en cualquier servidor Linux (Render, Docker, etc.).

Cada tipo de equipo se resuelve así:
  * Bomba de infusión y concentrador de O2: plantilla propia que replica el
    formato Word original (``pdf/bomba_infusion.html``, ``pdf/concentrador_o2.html``).
  * Resto de equipos: plantilla genérica (``pdf/protocolo_generico.html``) que se
    construye leyendo el propio formulario HTML del equipo, de modo que al
    completar un formulario nuevo su PDF aparece sin escribir más código.

Las hojas de vida de equipos (formato PM_003) usan ``pdf/hoja_de_vida.html``.
"""
import base64
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from flask import render_template

from database import db

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.abspath(os.path.join(BASE_DIR, '..'))
STATIC_DIR = os.path.realpath(os.path.join(APP_ROOT, 'static'))
FORMULARIOS_DIR = os.path.join(APP_ROOT, 'templates', 'forms', 'protocolos_mantenimiento')


# ----------------------------------------------------------------------------------------
# Excepciones
# ----------------------------------------------------------------------------------------
class ReporteNoEncontrado(LookupError):
    """El reporte no existe o no tiene datos."""


class InformeInvalido(ValueError):
    """Los datos guardados del reporte no se pueden interpretar."""


class TipoNoSoportado(ValueError):
    """No hay plantilla de PDF para ese tipo de equipo."""


# ----------------------------------------------------------------------------------------
# Registro de tipos de equipo (el id coincide con la tabla tipo_equipo y con app.py)
# codigo/version salen de los formatos Word (Formatos/*.docx); None = sin formato Word.
# ----------------------------------------------------------------------------------------
def _tipo(clave, titulo, codigo, version, formulario, plantilla='pdf/protocolo_generico.html'):
    return {
        'clave': clave, 'titulo': titulo, 'codigo': codigo, 'version': version,
        'formulario': formulario, 'plantilla': plantilla,
    }


TIPOS_EQUIPO = {
    1: _tipo('bascula', 'BÁSCULA', None, None, 'bascula.html'),
    2: _tipo('bomba_infusion', 'BOMBA DE INFUSIÓN', 'PM-020', 3, 'bomba_infusion.html',
             'pdf/bomba_infusion.html'),
    3: _tipo('cama_hospitalaria', 'CAMA HOSPITALARIA', 'PM-019', 3, 'cama_hospitalaria.html'),
    4: _tipo('concentrador_o2', 'CONCENTRADOR DE OXÍGENO', 'PM-013', 3, 'concentrador_O2.html',
             'pdf/concentrador_o2.html'),
    5: _tipo('desfibrilador', 'DESFIBRILADOR', 'PM-015', 3, 'desfibrilador.html'),
    6: _tipo('electrobisturi', 'ELECTROBISTURÍ', None, None, 'electrobisturi.html'),
    7: _tipo('electrocardiografo', 'ELECTROCARDIÓGRAFO', 'PM-017', 3, 'electrocardiografo.html'),
    8: _tipo('flujometro', 'FLUJÓMETRO / REGULADOR FLUJÓMETRO', 'PM-018', 3, 'flujometro.html'),
    9: _tipo('incubadora', 'INCUBADORA', None, None, 'incubadora.html'),
    10: _tipo('maquina_anestesia', 'MÁQUINA DE ANESTESIA', 'PM-012', 3, 'maquina_anestesia.html'),
    11: _tipo('monitor_signos_vitales', 'MONITOR DE SIGNOS VITALES', 'PM-011', 3,
              'monitor_signos_vitales.html'),
    12: _tipo('neveras', 'NEVERAS', None, None, 'neveras.html'),
    13: _tipo('oximetro', 'PULSOXÍMETRO', 'PM-011', 3, 'oximetro.html'),
    14: _tipo('succionador', 'SUCCIONADOR PORTÁTIL / VACUTRON', 'PM-014', 2, 'succionador.html'),
    15: _tipo('tensiometro', 'TENSIÓMETRO', 'PM-016', 3, 'tensiometro.html'),
    16: _tipo('ventilador', 'VENTILADOR PULMONAR', 'PM-012', 3, 'ventilador.html'),
}

ESTADOS = {
    'cumple': 'Cumple', 'no_cumple': 'No cumple', 'no cumple': 'No cumple',
    'na': 'No aplica', 'no aplica': 'No aplica',
    'nr': 'No registra', 'no registra': 'No registra',
    'si': 'Cumple', 'no': 'No cumple',
    'funcional': 'Funciona correctamente', 'parcial': 'Funciona parcialmente',
    'fuera_servicio': 'Fuera de servicio',
}


# ----------------------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------------------
def _texto(valor):
    if valor is None:
        return ''
    if isinstance(valor, (list, tuple)):
        return ', '.join(str(v) for v in valor)
    return str(valor)


def _legible(valor):
    """Convierte códigos guardados ('no_cumple') en texto para el PDF."""
    texto = _texto(valor).strip()
    return ESTADOS.get(texto.lower(), texto)


def _fecha(valor):
    """'2026-10-14' -> '14/10/2026'. Acepta date/datetime; si no se reconoce, se deja tal cual."""
    if hasattr(valor, 'strftime'):          # date / datetime que entrega PostgreSQL
        return valor.strftime('%d/%m/%Y')
    texto = _texto(valor).strip()
    for formato in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(texto, formato).strftime('%d/%m/%Y')
        except ValueError:
            continue
    return texto


fecha_legible = _fecha   # uso público (historial)


def _num(valor):
    """Primer número de un texto ('19,8 ml' -> 19.8); None si no hay."""
    coincidencia = re.search(r'-?\d+(?:[.,]\d+)?', _texto(valor))
    return float(coincidencia.group().replace(',', '.')) if coincidencia else None


def _fmt(numero, decimales=1):
    return '' if numero is None else f"{numero:.{decimales}f}"


def _nombre(catalogo, valor):
    """Busca el nombre por id REAL (no por posición en la lista)."""
    if valor in (None, ''):
        return ''
    for fila in catalogo or []:
        if str(fila[0]) == str(valor):
            return _texto(fila[1])
    return ''


def _items(data, definiciones, inicio=1):
    """[(texto, clave)] -> [{'n', 'texto', 'valor', 'incumple'}] numerados."""
    filas = []
    for n, (texto, clave) in enumerate(definiciones, start=inicio):
        valor = _legible(data.get(clave, ''))
        filas.append({'n': n, 'texto': texto, 'valor': valor,
                    'incumple': valor.lower() == 'no cumple'})
    return filas


# ----------------------------------------------------------------------------------------
# Recursos del PDF (logo, fuentes)
# ----------------------------------------------------------------------------------------
def pdf_asset(ruta):
    """URI ``file://`` de un archivo dentro de ``static/`` (impide salirse de esa carpeta)."""
    destino = os.path.realpath(os.path.join(STATIC_DIR, ruta))
    if not destino.startswith(STATIC_DIR + os.sep):
        raise ValueError(f"Recurso fuera de la carpeta static: {ruta}")
    if not os.path.isfile(destino):
        logger.warning("No se encontró el recurso del PDF: %s", ruta)
        return ''
    return Path(destino).as_uri()


# ----------------------------------------------------------------------------------------
# Lectura del formulario HTML (para los equipos sin plantilla propia)
# ----------------------------------------------------------------------------------------
class _LectorFormulario(HTMLParser):
    """Extrae secciones (h3/h4), campos con name, su <label> y las opciones de <select>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.titulo = ''
        self.secciones = []
        self._modo = None
        self._buffer = []
        self._label = ''
        self._select = None
        self._opcion = None

    def _seccion(self):
        if not self.secciones:
            self.secciones.append({'titulo': 'Verificaciones', 'campos': []})
        return self.secciones[-1]

    def _cerrar_texto(self):
        texto = ' '.join(''.join(self._buffer).split())
        self._buffer, self._modo = [], None
        return texto

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('h2', 'h3', 'h4', 'label', 'option'):
            self._modo, self._buffer = tag, []
            if tag == 'option':
                self._opcion = a.get('value')
        elif tag in ('input', 'select', 'textarea'):
            nombre = a.get('name')
            if nombre and a.get('type') not in ('hidden', 'submit', 'button'):
                campo = {'name': nombre, 'label': self._label or nombre, 'opciones': {}}
                self._seccion()['campos'].append(campo)
                self._label = ''
                if tag == 'select':
                    self._select = campo

    def handle_endtag(self, tag):
        if tag != self._modo:
            if tag == 'select':
                self._select = None
            return
        texto = self._cerrar_texto()
        if tag == 'h2':
            self.titulo = texto
        elif tag in ('h3', 'h4'):
            self.secciones.append({'titulo': texto, 'campos': []})
        elif tag == 'label':
            self._label = texto
        elif tag == 'option' and self._select is not None and self._opcion not in (None, ''):
            self._select['opciones'][self._opcion] = texto

    def handle_data(self, data):
        if self._modo:
            self._buffer.append(data)


@lru_cache(maxsize=64)
def _leer_formulario(ruta, mtime):
    """Cacheado por (ruta, fecha de modificación) para refrescarse si se edita el formulario."""
    with open(ruta, encoding='utf-8') as archivo:
        lector = _LectorFormulario()
        lector.feed(archivo.read())
    return lector.secciones


def _secciones_genericas(tipo, data):
    ruta = os.path.join(FORMULARIOS_DIR, tipo['formulario'])
    if not os.path.isfile(ruta):
        return []
    secciones = _leer_formulario(ruta, os.path.getmtime(ruta))

    vistos = defaultdict(int)
    resultado = []
    for seccion in secciones:
        filas = []
        for campo in seccion['campos']:
            posicion = vistos[campo['name']]
            vistos[campo['name']] += 1
            bruto = data.get(campo['name'], '')
            if isinstance(bruto, list):          # campos repetidos con el mismo name
                bruto = bruto[posicion] if posicion < len(bruto) else ''
            elif posicion > 0:
                bruto = ''
            valor = campo['opciones'].get(_texto(bruto), _texto(bruto))
            filas.append({'texto': campo['label'], 'valor': valor,
                        'incumple': valor.strip().lower() == 'no cumple'})
        if filas:
            resultado.append({'titulo': seccion['titulo'], 'filas': filas})
    return resultado


# ----------------------------------------------------------------------------------------
# Carga y contexto común
# ----------------------------------------------------------------------------------------
def _cargar_reporte(reporte_id):
    fila = db.obtener_reporte_por_id(reporte_id)
    if not fila:
        raise ReporteNoEncontrado(f"No existe el reporte {reporte_id}")
    bruto = fila[5]
    if isinstance(bruto, dict):                    # columna jsonb: el driver ya lo convierte
        data = bruto
    else:
        if isinstance(bruto, (bytes, bytearray)):
            bruto = bruto.decode('utf-8')
        if not bruto:
            raise ReporteNoEncontrado(f"El reporte {reporte_id} no tiene datos")
        try:
            data = json.loads(bruto)
        except (TypeError, ValueError) as error:
            raise InformeInvalido(f"El reporte {reporte_id} tiene un JSON inválido") from error
    if not isinstance(data, dict) or not data:
        raise InformeInvalido(f"El reporte {reporte_id} no tiene el formato esperado")
    return fila, data


def _contexto_comun(fila, data):
    repuestos = [
        {'cantidad': _texto(data.get(f'repuestos.cantidad_{i}', '')),
        'descripcion': _texto(data.get(f'repuestos.descripcion_{i}', ''))}
        for i in range(1, 6)
    ]
    return {
        'num_mant': fila[0],
        'fecha': _fecha(data.get('general.fecha_mantenimiento') or fila[4]),
        'cliente': _nombre(db.clientes(), data.get('cliente.nombre') or fila[2]),
        'area': _texto(data.get('cliente.area', '')).replace('_', ' '),
        'marca': _nombre(db.marcas(), data.get('equipo.marca')),
        'modelo': _texto(data.get('equipo.modelo', '')),
        'serie': _texto(data.get('equipo.numero_serie', '')),
        'inventario': _texto(data.get('equipo.numero_inventario', '')),
        'servicio': _nombre(db.servicios(), data.get('servicio.tipo')),
        'limpieza': _legible(data.get('servicio.limpieza', '')),
        'actividad': _texto(data.get('servicio.actividad', '')),
        'observaciones': _texto(data.get('servicio.observaciones', '')),
        'repuestos': repuestos,
        'estado_final': _legible(data.get('resultado.estado', '')),
        'profesional': _nombre(db.personal(), data.get('tecnico.nombre') or fila[1]),
    }


# ----------------------------------------------------------------------------------------
# Bomba de infusión
# ----------------------------------------------------------------------------------------
PROGRAMADO_BOMBA = {'volumen': 20, 'tiempo': 20, 'flujo': 60}   # ml, min, ml/h (como el Word)
TOLERANCIA_BOMBA_PCT = 5


def _contexto_bomba(data, comun):
    p = 'protocolo_infusion.'
    volumen_medido = data.get(p + 'volumen_medio', '')
    tiempo_medido = data.get(p + 'tiempo_medio', '')
    v, t = _num(volumen_medido), _num(tiempo_medido)
    flujo = (v / t * 60) if (v is not None and t) else None   # ml/h

    def error(medido, programado):
        if medido is None:
            return None, None
        diferencia = medido - programado
        return diferencia, abs(diferencia) / programado * 100

    ev, pv = error(v, PROGRAMADO_BOMBA['volumen'])
    et, pt = error(t, PROGRAMADO_BOMBA['tiempo'])
    ef, pf = error(flujo, PROGRAMADO_BOMBA['flujo'])
    porcentajes = [x for x in (pv, pt, pf) if x is not None]
    cumple = ('' if not porcentajes else
            'Sí' if all(x <= TOLERANCIA_BOMBA_PCT for x in porcentajes) else 'No')

    inspeccion = _items(data, [
        ('Efectuar limpieza interna y externa del equipo.', 'servicio.limpieza'),
        ('Verificación de pantalla, botones de selección.', p + 'pantalla_botones'),
        ('Verificación del sensor de gotas.', p + 'sensor_gotas'),
        ('Verificación de cable de poder.', p + 'cable_poder'),
        ('Verificación de la puerta.', p + 'puerta'),
    ])
    alarmas = _items(data, [
        ('Alarma de OCLUSIÓN.', p + 'alarma_oclusion'),
        ('Alarma de AIRE EN LA LÍNEA', p + 'alarma_aire'),
        ('Alarma de FRASCO VACÍO / PRESIÓN DISTAL', p + 'alarma_frasco_vacio'),
        ('Alarma de FLUJO LIBRE', p + 'alarma_flujo_libre'),
        ('Alarma de PUERTA ABIERTA', p + 'alarma_puerta'),
        ('Pre-alarma de infusión y alarma de FIN DE INFUSIÓN', p + 'fin_infusion'),
    ], inicio=6)

    return {
        'inspeccion': inspeccion,
        'alarmas': alarmas,
        'prog': PROGRAMADO_BOMBA,
        'tolerancia': TOLERANCIA_BOMBA_PCT,
        'cumple': cumple,
        'med': {
            'volumen': _texto(volumen_medido), 'tiempo': _texto(tiempo_medido), 'flujo': _fmt(flujo),
            'error_volumen': _fmt(ev), 'pct_volumen': _fmt(pv),
            'error_tiempo': _fmt(et), 'pct_tiempo': _fmt(pt),
            'error_flujo': _fmt(ef), 'pct_flujo': _fmt(pf),
        },
    }


# ----------------------------------------------------------------------------------------
# Concentrador de oxígeno
# ----------------------------------------------------------------------------------------
TOLERANCIA_FLUJO_LPM = 1.0      # "Flujo ± 1,0 L/min" en el formato Word
MINIMO_O2_PCT = 89.0            # "Concentración de O2 > 89%" en el formato Word


def _contexto_concentrador(data, comun):
    inspeccion = _items(data, [
        ('Efectuar limpieza interna y externa del equipo.', 'servicio.limpieza'),
        ('Verificación de pantalla, botón de encendido', 'verificación de pantalla, botón de encendido.'),
        ('Verificación de llantas de transporte.', 'verificación de llantas de transporte.'),
        ('Verificación de flujómetro', 'verificación de flujómetro.'),
        ('Verificación de cable AC.', 'verificación de cable A.C.'),
        ('Verificación de batería.', 'verificación de bateria.'),
        ('Horas de funcionamiento de turbina y/o compresor.',
        'Horas funcionamiento de turbina y/o compresor.'),
        ('Cambio de filtro de entrada de aire.', 'Cambio de filtro de entrada de aire.'),
        ('Porcentaje de concentración de oxígeno.', 'Porcentaje de concentración de oxígeno.'),
    ])
    alarmas = _items(data, [
        ('Alarmas sonoras.', 'Alarmas sonoras.'),
        ('Alarmas visuales (leds).', 'Alarmas visuales (leds).'),
    ], inicio=10)

    flujos = []
    for programado in range(1, 11):
        bruto = data.get(f'Flujo {programado}', '')
        medido = _num(bruto)
        if medido is None:
            error_, estado = '', ''
        else:
            diferencia = medido - programado
            error_ = f"{diferencia:+.1f}"
            estado = 'Cumple' if abs(diferencia) <= TOLERANCIA_FLUJO_LPM else 'No cumple'
        flujos.append({'programado': programado, 'medido': _texto(bruto).strip(),
                    'error': error_, 'estado': estado, 'incumple': estado == 'No cumple'})

    o2_bruto = data.get('concentrador.o2_medido', '')
    o2 = _num(o2_bruto)
    o2_estado = '' if o2 is None else ('Cumple' if o2 > MINIMO_O2_PCT else 'No cumple')

    return {
        'inspeccion': inspeccion,
        'alarmas': alarmas,
        'flujos': flujos,
        'tolerancia_flujo': TOLERANCIA_FLUJO_LPM,
        'o2': {'medido': _texto(o2_bruto).strip(), 'estado': o2_estado,
            'incumple': o2_estado == 'No cumple', 'minimo': MINIMO_O2_PCT},
    }


# ----------------------------------------------------------------------------------------
# API pública
# ----------------------------------------------------------------------------------------
def construir_contexto(reporte_id):
    """Devuelve ``(plantilla, contexto)`` para un reporte. No necesita WeasyPrint."""
    fila, data = _cargar_reporte(reporte_id)

    bruto_tipo = data.get('equipo.tipo') or fila[3]
    try:
        tipo = TIPOS_EQUIPO[int(bruto_tipo)]
    except (KeyError, TypeError, ValueError):
        raise TipoNoSoportado(f"No hay plantilla PDF para el tipo de equipo {bruto_tipo!r}")

    contexto = _contexto_comun(fila, data)
    contexto['tipo'] = tipo
    contexto['asset'] = pdf_asset

    if tipo['clave'] == 'bomba_infusion':
        contexto.update(_contexto_bomba(data, contexto))
    elif tipo['clave'] == 'concentrador_o2':
        contexto.update(_contexto_concentrador(data, contexto))
    else:
        contexto['secciones'] = _secciones_genericas(tipo, data)
        # La limpieza general ya viene en el formulario de varios equipos; solo se muestra
        # la del bloque común cuando el formulario específico no la incluye.
        contexto['mostrar_limpieza_comun'] = not any(
            'limpieza' in fila['texto'].lower()
            for seccion in contexto['secciones'] for fila in seccion['filas'])

    return tipo['plantilla'], contexto


def generar_reporte_html(reporte_id):
    """HTML final del informe (útil para depurar el diseño en el navegador)."""
    plantilla, contexto = construir_contexto(reporte_id)
    return render_template(plantilla, **contexto)


def generar_reporte_pdf(reporte_id):
    """Genera el PDF en memoria y devuelve ``bytes`` (no se escriben archivos temporales)."""
    return _html_a_pdf(generar_reporte_html(reporte_id))   # Jinja2 escapa los datos del usuario


# ----------------------------------------------------------------------------------------
# Hoja de vida de equipo médico (formato PM_003)
# ----------------------------------------------------------------------------------------
MESES_HV = ('Ene.', 'Feb.', 'Mar.', 'Abr.', 'May.', 'Jun.',
            'Jul.', 'Ago.', 'Sep.', 'Oct.', 'Nov.', 'Dic.')
FILAS_ACCESORIOS_HV = 8
FECHA_APROBACION_HV = ''        # fecha de aprobación del formato PM_003 (ej. '1/11/2021')
SI_NO = {'si': 'Sí', 'no': 'No'}


def nombre_archivo_hoja_vida(inventario, hoja_id):
    """'hoja_vida_MED_0001.pdf' (solo caracteres seguros para un nombre de archivo)."""
    base = re.sub(r'[^A-Za-z0-9_.-]', '_', _texto(inventario).strip())[:80].strip('._')
    return f"hoja_vida_{base or hoja_id}.pdf"


def contexto_hoja_vida(hoja_id):
    """Datos de la hoja de vida listos para ``pdf/hoja_de_vida.html``."""
    hoja = db.obtener_hoja_vida(hoja_id)
    if not hoja:
        raise ReporteNoEncontrado(f"No existe la hoja de vida {hoja_id}")

    por_fila = {a['fila']: a for a in hoja.get('accesorios') or []}
    meses = set(hoja.get('meses_mantenimiento') or [])
    foto = ''
    if hoja.get('foto') and hoja.get('foto_mime') in ('image/jpeg', 'image/png'):
        foto = f"data:{hoja['foto_mime']};base64,{base64.b64encode(bytes(hoja['foto'])).decode('ascii')}"

    return {
        'hv': {clave: ('' if valor is None else valor) for clave, valor in hoja.items()},
        'fecha_adquisicion': _fecha(hoja.get('fecha_adquisicion')),
        'meses': [(nombre, numero in meses) for numero, nombre in enumerate(MESES_HV, start=1)],
        'accesorios': [por_fila.get(n, {}) for n in range(1, FILAS_ACCESORIOS_HV + 1)],
        'foto': foto,
        'si_no': lambda valor: SI_NO.get(_texto(valor), ''),
        'fecha_aprobacion': FECHA_APROBACION_HV,
        'asset': pdf_asset,
    }


def generar_hoja_vida_pdf(hoja_id):
    """Devuelve ``(bytes del PDF, nombre de archivo)`` de una hoja de vida."""
    contexto = contexto_hoja_vida(hoja_id)
    pdf = _html_a_pdf(render_template('pdf/hoja_de_vida.html', **contexto))
    return pdf, nombre_archivo_hoja_vida(contexto['hv']['numero_inventario'], hoja_id)


# ----------------------------------------------------------------------------------------
# Reporte general / reporte de servicio (formato FT-GC-041, Formatos/REPORTE DE SERVICIO.pdf)
# ----------------------------------------------------------------------------------------
FORMATO_REPORTE_GENERAL = {
    'empresa': 'MULTILINK INGENIERIA SAS',
    'logo': 'images/pdf_logo.png',          # cambia por el logo de la empresa si lo agregas a static/images
    'codigo': 'FT-GC-041',
    'version': '003',
    'fecha_revision': 'MARZO/2026',
    'fecha_aprobacion': '2024 - 08 - 01',
}
FILAS_REPUESTOS_RG = 5


def nombre_archivo_reporte_general(reporte_id, inventario=''):
    """'reporte_servicio_15_INV_0040.pdf' (solo caracteres seguros para un nombre de archivo)."""
    base = re.sub(r'[^A-Za-z0-9_.-]', '_', _texto(inventario).strip())[:80].strip('._')
    return f"reporte_servicio_{reporte_id}{'_' + base if base else ''}.pdf"


def contexto_reporte_general(reporte_id):
    """Datos del reporte general listos para ``pdf/reporte_general.html``."""
    fila = db.obtener_reporte_general(reporte_id)
    if not fila:
        raise ReporteNoEncontrado(f"No existe el reporte general {reporte_id}")

    info = fila.get('informacion')
    if not isinstance(info, dict):              # jsonb llega como dict; cualquier otra cosa se ignora
        info = {}
    equipo = info.get('equipo') if isinstance(info.get('equipo'), dict) else {}

    def nombre(dato):
        """Los catálogos se guardan como {'id', 'nombre'}; acepta también texto plano."""
        return _texto(dato.get('nombre') if isinstance(dato, dict) else dato)

    repuestos = [r for r in info.get('repuestos') or [] if isinstance(r, dict)][:FILAS_REPUESTOS_RG]
    repuestos += [{}] * (FILAS_REPUESTOS_RG - len(repuestos))
    ubicacion = ' / '.join(t for t in (_texto(info.get('departamento')), _texto(fila['ciudad'])) if t)

    return {
        'f': FORMATO_REPORTE_GENERAL,
        'rg': {
            'numero': fila['id'],
            'fecha': _fecha(info.get('fecha')),
            'cliente': _texto(fila['cliente']),
            'solicitante': _texto(fila['solicitante']),
            'area': _texto(fila['area']),
            'ubicacion': ubicacion,
            'direccion': _texto(info.get('direccion')),
            'equipo': nombre(equipo.get('tipo_equipo')),
            'marca': nombre(equipo.get('marca')),
            'modelo': _texto(equipo.get('modelo')),
            'serie': _texto(equipo.get('serie') or fila['serie_inventario']),
            'inventario': _texto(equipo.get('inventario')),
            'servicio': _texto(fila['servicio']),
            'descripcion': _texto(info.get('descripcion')),
            'diagnostico': _texto(info.get('diagnostico')),
            'actividad': _texto(info.get('actividad')),
            'observaciones': _texto(info.get('observaciones')),
            'estado_final': _texto(info.get('estado_final')),
            'fecha_entrega': _fecha(fila['fecha_entrega']),
            'recibido_por': _texto(info.get('recibido_por')),
            'tecnico': _texto(fila['tecnico']),
            'tecnico_cargo': _texto(fila['tecnico_cargo']),
        },
        'repuestos': repuestos,
        'asset': pdf_asset,
    }


def generar_reporte_general_pdf(reporte_id):
    """Devuelve ``(bytes del PDF, nombre de archivo)`` de un reporte general."""
    contexto = contexto_reporte_general(reporte_id)
    pdf = _html_a_pdf(render_template('pdf/reporte_general.html', **contexto))
    return pdf, nombre_archivo_reporte_general(reporte_id, contexto['rg']['inventario'])


# ----------------------------------------------------------------------------------------
# Documentos administrativos: factura, orden de compra, cuenta de cobro, desprendible de
# nómina y reporte de metrología. Todos usan la plantilla genérica ``pdf/documento.html``:
# cada función solo arma el diccionario ``doc`` (bloques de datos, tabla, totales y firmas).
# ----------------------------------------------------------------------------------------
EMPRESA_DOCUMENTOS = {
    'nombre': 'BIOMEGA S.A.S.',
    'nit': '',                                # NIT de la empresa (aparece en los documentos si se llena)
    'logo': 'images/pdf_logo.png',
}
_PREFIJOS = {'factura': 'FAC', 'orden': 'OC', 'cuenta': 'CC', 'nomina': 'NOM', 'metrologia': 'MET'}
_NOMBRES_MES = ('', 'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto',
                'septiembre', 'octubre', 'noviembre', 'diciembre')


def numero_documento(prefijo, documento_id):
    """('FAC', 7) -> 'FAC-000007'. Acepta la clave ('factura') o el prefijo ('FAC')."""
    return f"{_PREFIJOS.get(prefijo, prefijo)}-{int(documento_id):06d}"


def pesos(valor):
    """1234567.5 -> '$ 1.234.567,50' (formato colombiano); '' si no hay valor."""
    if valor in (None, ''):
        return ''
    try:
        texto = f"{float(valor):,.2f}" if not hasattr(valor, 'quantize') else f"{valor:,.2f}"
    except (TypeError, ValueError):
        return _texto(valor)
    return '$ ' + texto.replace(',', '_').replace('.', ',').replace('_', '.')


def _cantidad(valor):
    """Decimal('2.00') -> '2'; Decimal('1.50') -> '1,5'."""
    texto = f"{valor:f}".rstrip('0').rstrip('.') if hasattr(valor, 'quantize') else _texto(valor)
    return texto.replace('.', ',')


def _nombre_archivo(prefijo, documento_id, extra=''):
    base = re.sub(r'[^A-Za-z0-9_.-]', '_', _texto(extra).strip())[:60].strip('._')
    return f"{numero_documento(prefijo, documento_id)}{'_' + base if base else ''}.pdf"


def _render_documento(doc):
    return _html_a_pdf(render_template('pdf/documento.html', doc=doc, empresa=EMPRESA_DOCUMENTOS,
                                       asset=pdf_asset))


def _tabla_items(items):
    filas = []
    for item in items or []:
        bruto = item['cantidad'] * item['valor_unitario']
        filas.append([_cantidad(item['cantidad']), _texto(item['descripcion']), pesos(item['valor_unitario']),
                      f"{_cantidad(item['iva_pct'])} %", pesos(bruto)])
    return {'titulo': 'Detalle', 'filas': filas,
            'columnas': [('Cant.', 'c', 9), ('Descripción', 'l', 45), ('Valor unitario', 'r', 17),
                         ('IVA', 'c', 9), ('Valor', 'r', 20)]}


def _totales_documento(d):
    return [('Subtotal', pesos(d['subtotal']), False), ('IVA', pesos(d['iva']), False),
            ('TOTAL', pesos(d['total']), True)]


def generar_factura_pdf(factura):
    """``factura``: db.obtener_factura(). Devuelve (bytes, nombre de archivo)."""
    ubicacion = ' / '.join(t for t in (_texto(factura.get('departamento')), _texto(factura.get('ciudad'))) if t)
    doc = {
        'titulo': 'FACTURA DE VENTA', 'numero': numero_documento('factura', factura['id']),
        'subtitulo': 'Documento de control interno · no reemplaza la factura electrónica DIAN',
        'bloques': [
            {'titulo': 'Cliente', 'pares': [('Razón social', factura.get('cliente')), ('NIT', factura.get('nit')),
                                             ('Dirección', factura.get('direccion')), ('Ciudad', ubicacion),
                                             ('Teléfono', factura.get('telefono')), ('Correo', factura.get('correo'))]},
            {'titulo': 'Factura', 'pares': [('Fecha de emisión', _fecha(factura['fecha_emision'])),
                                             ('Fecha de vencimiento', _fecha(factura['fecha_vencimiento'])),
                                             ('Estado', _texto(factura['estado']).capitalize())]},
        ],
        'tabla': _tabla_items(factura.get('items')),
        'totales': _totales_documento(factura),
        'textos': [('Observaciones', factura.get('observaciones'))],
        'firmas': ['Elaborado por', 'Recibido / aceptado por el cliente'],
    }
    return _render_documento(doc), _nombre_archivo('factura', factura['id'], factura.get('cliente'))


def generar_orden_pdf(orden):
    doc = {
        'titulo': 'ORDEN DE COMPRA', 'numero': numero_documento('orden', orden['id']),
        'bloques': [
            {'titulo': 'Proveedor', 'pares': [('Proveedor', orden.get('proveedor')), ('NIT', orden.get('nit_proveedor')),
                                               ('Contacto', orden.get('contacto'))]},
            {'titulo': 'Orden', 'pares': [('Fecha', _fecha(orden['fecha'])),
                                           ('Fecha de entrega', _fecha(orden.get('fecha_entrega'))),
                                           ('Estado', _texto(orden['estado']).capitalize())]},
        ],
        'tabla': _tabla_items(orden.get('items')),
        'totales': _totales_documento(orden),
        'textos': [('Observaciones', orden.get('observaciones'))],
        'firmas': ['Elaborado por', 'Aprobado por', 'Recibido por el proveedor'],
    }
    return _render_documento(doc), _nombre_archivo('orden', orden['id'], orden.get('proveedor'))


def generar_cuenta_cobro_pdf(cuenta):
    doc = {
        'titulo': 'CUENTA DE COBRO', 'numero': numero_documento('cuenta', cuenta['id']),
        'bloques': [
            {'titulo': 'Debe a', 'pares': [('Nombre', cuenta.get('empleado')), ('Documento', cuenta.get('documento')),
                                            ('Cargo / profesión', cuenta.get('cargo') or cuenta.get('profesion')),
                                            ('Teléfono', cuenta.get('telefono')), ('Correo', cuenta.get('correo'))]},
            {'titulo': 'Cuenta', 'pares': [('Fecha', _fecha(cuenta['fecha'])),
                                            ('Periodo', f"{_fecha(cuenta['periodo_inicio'])} al {_fecha(cuenta['periodo_fin'])}"),
                                            ('Estado', _texto(cuenta['estado']).capitalize())]},
        ],
        'textos': [('Concepto', cuenta.get('concepto')), ('Observaciones', cuenta.get('observaciones'))],
        'totales': [('VALOR A PAGAR', pesos(cuenta['valor']), True)],
        'leyenda': f"{EMPRESA_DOCUMENTOS['nombre']} debe a {_texto(cuenta.get('empleado'))} la suma indicada "
                   "por el concepto descrito.",
        'firmas': ['Firma de quien cobra', 'Aprobado por'],
    }
    return _render_documento(doc), _nombre_archivo('cuenta', cuenta['id'], cuenta.get('documento'))


def generar_nomina_pdf(registro):
    periodo = registro['periodo']
    salario_periodo = registro['salario_base'] * registro['dias_trabajados'] / 30
    auxilio_periodo = registro['auxilio_transporte'] * registro['dias_trabajados'] / 30
    filas = [
        ['Salario básico', f"{registro['dias_trabajados']} días", pesos(salario_periodo), ''],
        ['Auxilio de transporte', '', pesos(auxilio_periodo), ''],
        ['Horas extra y recargos', '', pesos(registro['horas_extra']), ''],
        ['Bonificaciones', '', pesos(registro['bonificaciones']), ''],
        ['Aporte a salud (4 %)', '', '', pesos(registro['salud'])],
        ['Aporte a pensión (4 %)', '', '', pesos(registro['pension'])],
        ['Otras deducciones', '', '', pesos(registro['otras_deducciones'])],
    ]
    doc = {
        'titulo': 'DESPRENDIBLE DE NÓMINA', 'numero': numero_documento('nomina', registro['id']),
        'bloques': [
            {'titulo': 'Empleado', 'pares': [('Nombre', registro.get('empleado')), ('Documento', registro.get('documento')),
                                              ('Cargo', registro.get('cargo'))]},
            {'titulo': 'Periodo', 'pares': [('Mes', f"{_NOMBRES_MES[periodo.month].capitalize()} de {periodo.year}"),
                                             ('Salario base mensual', pesos(registro['salario_base'])),
                                             ('Días trabajados', registro['dias_trabajados'])]},
        ],
        'tabla': {'titulo': 'Liquidación', 'filas': filas,
                  'columnas': [('Concepto', 'l', 40), ('Detalle', 'c', 16), ('Devengado', 'r', 22),
                               ('Deducido', 'r', 22)]},
        'totales': [('Total devengado', pesos(registro['total_devengado']), False),
                    ('Total deducciones', pesos(registro['total_deducciones']), False),
                    ('NETO A PAGAR', pesos(registro['neto_pagar']), True)],
        'textos': [('Observaciones', registro.get('observaciones'))],
        'firmas': ['Empleador', 'Recibí conforme'],
    }
    return _render_documento(doc), _nombre_archivo('nomina', registro['id'],
                                                   f"{periodo:%Y-%m}_{_texto(registro.get('documento'))}")


TIPOS_METROLOGIA = {'calibracion': 'Calibración', 'verificacion': 'Verificación', 'calificacion': 'Calificación'}


def generar_metrologia_pdf(reporte):
    unidad = _texto(reporte.get('unidad'))
    mediciones = reporte.get('mediciones') if isinstance(reporte.get('mediciones'), list) else []
    filas = [[m.get('n'), f"{m.get('patron')} {unidad}", f"{m.get('medido')} {unidad}",
              f"{m.get('error')} {unidad}", f"± {m.get('tolerancia')} {unidad}",
              'Cumple' if m.get('cumple') else 'No cumple'] for m in mediciones if isinstance(m, dict)]
    conforme = reporte.get('resultado') == 'conforme'
    doc = {
        'titulo': f"REPORTE DE {TIPOS_METROLOGIA.get(reporte['tipo'], 'METROLOGÍA').upper()}",
        'numero': numero_documento('metrologia', reporte['id']),
        'bloques': [
            {'titulo': 'Equipo', 'pares': [('Equipo', reporte.get('equipo')), ('Marca', reporte.get('marca')),
                                            ('Serie', reporte.get('serie')), ('N° inventario', reporte.get('inventario')),
                                            ('Cliente', reporte.get('cliente'))]},
            {'titulo': 'Actividad', 'pares': [('Fecha', _fecha(reporte['fecha'])),
                                               ('Magnitud', f"{_texto(reporte.get('magnitud'))} ({unidad})"),
                                               ('Equipo patrón', reporte.get('patron')),
                                               ('Certificado del patrón', reporte.get('certificado_patron')),
                                               ('Próxima fecha', _fecha(reporte.get('proxima_fecha')))]},
        ],
        'tabla': {'titulo': 'Mediciones', 'filas': filas,
                  'columnas': [('N°', 'c', 6), ('Valor patrón', 'c', 19), ('Valor medido', 'c', 19),
                               ('Error', 'c', 18), ('Tolerancia', 'c', 18), ('Resultado', 'c', 20)]},
        'totales': [('RESULTADO', 'CONFORME' if conforme else 'NO CONFORME', True)],
        'textos': [('Observaciones', reporte.get('observaciones'))],
        'leyenda': "Error = valor medido − valor patrón. Una medición cumple si |error| ≤ tolerancia; "
                   "el equipo es conforme si todas las mediciones cumplen.",
        'firmas': [f"Realizado por: {_texto(reporte.get('tecnico'))}", 'Revisado por'],
    }
    return _render_documento(doc), _nombre_archivo('metrologia', reporte['id'], reporte.get('inventario'))


# ----------------------------------------------------------------------------------------
# Conversión HTML -> PDF (compartida por reportes y hojas de vida)
# ----------------------------------------------------------------------------------------
def ruta_local_permitida(url):
    """True si ``url`` es ``data:`` o un ``file://`` dentro de ``static/``."""
    partes = urlsplit(url)
    if partes.scheme.lower() == 'data':
        return True
    if partes.scheme.lower() != 'file':
        return False
    return os.path.realpath(url2pathname(partes.path)).startswith(STATIC_DIR + os.sep)


def _html_a_pdf(html):
    """Convierte el HTML en PDF en memoria. WeasyPrint solo puede leer recursos de static/
    (y datos ``data:``): ni internet ni otros archivos del servidor."""
    # Import tardío: si faltan las librerías del sistema (Pango) la app sigue arrancando
    # y el error aparece solo al pedir un PDF, con un mensaje claro en el log.
    from weasyprint import HTML
    from weasyprint.urls import URLFetcher

    class FetcherSeguro(URLFetcher):
        def fetch(self, url, headers=None):
            if not ruta_local_permitida(url):
                logger.warning("Recurso bloqueado en el PDF: %s", url[:200])
                raise ValueError("Recurso no permitido en el PDF")
            return super().fetch(url, headers)

    return HTML(string=html, base_url=STATIC_DIR + os.sep,
                url_fetcher=FetcherSeguro(allowed_protocols=('file', 'data'))).write_pdf()

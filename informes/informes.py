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
"""
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path

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
    # Import tardío: si faltan las librerías del sistema (Pango) la app sigue arrancando
    # y el error aparece solo al pedir un PDF, con un mensaje claro en el log.
    from weasyprint import HTML

    html = generar_reporte_html(reporte_id)   # Jinja2 escapa todos los datos del usuario
    return HTML(string=html, base_url=STATIC_DIR + os.sep).write_pdf()

"""Entorno de pruebas: base de datos simulada (no hace falta MySQL ni WeasyPrint)."""
import json
import logging
import os
import sys
import types

logging.disable(logging.CRITICAL)   # silencia los avisos esperados de las pruebas

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

os.environ.update({
    'SECRET_KEY': 'clave-de-pruebas-' + 'x' * 32,
    'FLASK_DEBUG': 'true',
    'ADMIN_USER': 'admin',
    'DEFAULT_PASSWORD': 'Contraseña-de-prueba-1',
})
os.environ.pop('ADMIN_PASSWORD_HASH', None)

# --- base de datos simulada ---------------------------------------------------------------
TIPOS = [(i, n) for i, n in enumerate(
    ['Báscula', 'Bomba de infusión', 'Cama', 'Concentrador', 'Desfibrilador', 'Electrobisturí',
     'Electrocardiógrafo', 'Flujómetro', 'Incubadora', 'Máquina de anestesia', 'Monitor',
     'Neveras', 'Pulsoxímetro', 'Succionador', 'Tensiómetro', 'Ventilador'], start=1)]

REPORTES = {}


def reiniciar():
    REPORTES.clear()
    HOJAS.clear()
    INVENTARIO_GUARDADO.clear()


def guardar(reporte_id, equipo, datos, tecnico=1, cliente=7, fecha='2026-10-14'):
    REPORTES[reporte_id] = (reporte_id, tecnico, cliente, equipo, fecha, json.dumps(datos, ensure_ascii=False))


fake = types.ModuleType('database.db')
fake.personal = lambda: [(1, 'Juan Pérez'), (2, 'Ana Gómez')]
fake.clientes = lambda: [(3, 'Otro'), (7, 'Clínica <Norte> & Cía')]      # ids no consecutivos a propósito
fake.marcas = lambda: [(3, 'B. Braun')]
fake.servicios = lambda: [(1, 'Preventivo'), (2, 'Correctivo')]
fake.tipo_equipos = lambda: TIPOS
fake.obtener_reporte_por_id = lambda i: REPORTES.get(i)
fake.contar_reportes = lambda desde=None: len(REPORTES)
fake.listar_reportes = lambda limite=25, desplazamiento=0: [
    r[:5] for r in sorted(REPORTES.values(), key=lambda r: -r[0])][desplazamiento:desplazamiento + limite]


def _subir(tecnico, cliente, equipo, fecha, reporte):
    nuevo = max(REPORTES, default=0) + 1
    REPORTES[nuevo] = (nuevo, tecnico, cliente, equipo, fecha, reporte)
    return nuevo, equipo


fake.subir_reporte = _subir

# --- hojas de vida ---------------------------------------------------------------------------
INVENTARIO = [
    {'id': 4, 'serie': 'SN-77', 'inventario': 'MED_0077', 'equipo': 'Concentrador de O2',
     'marca': 'B. Braun', 'entidad': 'Clínica <Norte> & Cía', 'riesgo': 'IIA', 'invima': 'INV-2020'},
    {'id': 9, 'serie': 'SN-99', 'inventario': 'MED/0099', 'equipo': 'Monitor',
     'marca': None, 'entidad': 'Otro', 'riesgo': '', 'invima': None},
]
HOJAS = {}


class HojaVidaDuplicada(Exception):
    pass


def _guardar_hoja(datos, accesorios):
    if any(h['datos']['id_inventario'] == datos['id_inventario'] for h in HOJAS.values()):
        raise HojaVidaDuplicada()
    nuevo = max(HOJAS, default=0) + 1
    HOJAS[nuevo] = {'datos': datos, 'accesorios': accesorios}
    return nuevo


def _obtener_hoja(hoja_id):
    """Imita la vista v_hojas_vida (une inventario, marca, cliente y responsable)."""
    guardada = HOJAS.get(hoja_id)
    if not guardada:
        return None
    datos = guardada['datos']
    equipo = next(e for e in INVENTARIO if e['id'] == datos['id_inventario'])
    persona = {1: ('Juan Pérez', '3001234567', 'juan@ejemplo.com')}.get(datos.get('id_responsable'), (None,) * 3)
    marcas = dict(fake.marcas())
    hoja = dict(datos, id_hoja_vida=hoja_id, nombre_equipo=equipo['equipo'], marca=equipo['marca'],
                serie=equipo['serie'], numero_inventario=equipo['inventario'], entidad=equipo['entidad'],
                clasificacion_riesgo=equipo['riesgo'], responsable=persona[0],
                responsable_contacto=persona[1], responsable_email=persona[2])
    hoja['accesorios'] = [dict(a, marca=marcas.get(a['id_marca'])) for a in guardada['accesorios']]
    return hoja


fake.HojaVidaDuplicada = HojaVidaDuplicada
fake.inventario_sin_hoja_vida = lambda: [
    e for e in INVENTARIO if all(h['datos']['id_inventario'] != e['id'] for h in HOJAS.values())]
fake.responsables = lambda: [{'id': 1, 'nombre': 'Juan Pérez', 'telefono': '3001234567', 'correo': 'juan@ejemplo.com'},
                             {'id': 2, 'nombre': 'Ana Gómez', 'telefono': None, 'correo': 'ana@ejemplo.com'}]
fake.guardar_hoja_vida = _guardar_hoja
fake.obtener_hoja_vida = _obtener_hoja


def _buscar_hojas(texto=''):
    filas = []
    for hoja_id in sorted(HOJAS, reverse=True):
        h = _obtener_hoja(hoja_id)
        campos = (h['serie'], h['numero_inventario'], h['nombre_equipo'], h['entidad'], h['marca'])
        if not texto or any(texto.lower() in (c or '').lower() for c in campos):
            filas.append({'id': hoja_id, 'nombre_equipo': h['nombre_equipo'], 'marca': h['marca'],
                          'modelo': h.get('modelo'), 'serie': h['serie'], 'numero_inventario': h['numero_inventario'],
                          'entidad': h['entidad'], 'area_servicio': h.get('area_servicio'),
                          'fecha_creacion': '2026-10-23 09:15:00'})
    return filas


# --- inventario -------------------------------------------------------------------------------
INVENTARIO_GUARDADO = []


class InventarioDuplicado(Exception):
    def __init__(self, campo=None):
        super().__init__(campo)
        self.campo = campo


def _guardar_inventario(datos):
    """Imita el UNIQUE de la tabla: solo la serie."""
    for campo in ('serie',):
        if any(e[campo] == datos[campo] for e in INVENTARIO_GUARDADO):
            raise InventarioDuplicado(campo)
    INVENTARIO_GUARDADO.append(datos)
    return len(INVENTARIO_GUARDADO)


fake.InventarioDuplicado = InventarioDuplicado
fake.guardar_inventario = _guardar_inventario
fake.inventario = lambda: [
    {'id': 1, 'tipo_equipo': 'Monitor', 'marca': None, 'lote': 'L-9', 'serie': 'SN-9',
     'serial_inventario_interno': 'MED_0009', 'cliente': 'Clínica <Norte> & Cía', 'riesgo': 'IIB', 'invima': 'INV-9'}]
fake.riesgo_equipos = lambda: [(1, 'I'), (2, 'IIA'), (3, 'IIB'), (4, 'III')]

fake.buscar_hojas_vida = lambda texto='', limite=20, desplazamiento=0: \
    _buscar_hojas(texto)[desplazamiento:desplazamiento + limite]
fake.contar_hojas_vida = lambda texto='': len(_buscar_hojas(texto))

import database  # noqa: E402  (paquete de espacio de nombres del proyecto)
sys.modules['database.db'] = fake
database.db = fake

import app as aplicacion  # noqa: E402

sisem = aplicacion.sisem
informes = aplicacion.informes

DATOS_COMUNES = {
    'general.fecha_mantenimiento': '2026-10-14',
    'cliente.nombre': '7', 'cliente.area': 'UCI_intermédios',
    'equipo.marca': '3', 'equipo.modelo': 'Infusomat', 'equipo.numero_serie': 'SN-1',
    'equipo.numero_inventario': 'INV-9', 'servicio.tipo': '1', 'servicio.limpieza': 'cumple',
    'servicio.actividad': 'Línea 1\nLínea 2', 'servicio.observaciones': 'Sin novedades',
    'repuestos.cantidad_1': '2', 'repuestos.descripcion_1': 'Cable de poder',
    'resultado.estado': 'funcional', 'tecnico.nombre': '1',
}

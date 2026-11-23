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
    CRONOGRAMA.clear()


def guardar(reporte_id, equipo, datos, tecnico=1, cliente=7, fecha='2026-10-14'):
    REPORTES[reporte_id] = (reporte_id, tecnico, cliente, equipo, fecha, json.dumps(datos, ensure_ascii=False))


fake = types.ModuleType('database.db')
fake.personal = lambda: [(1, 'Juan Pérez'), (2, 'Ana Gómez')]
fake.clientes = lambda: [(3, 'Otro'), (7, 'Clínica <Norte> & Cía')]      # ids no consecutivos a propósito
fake.marcas = lambda: [(3, 'B. Braun')]
fake.servicios = lambda: [(1, 'Preventivo'), (2, 'Correctivo')]
fake.tipo_equipos = lambda: TIPOS
fake.areas = lambda: [(1, 'UCI'), (2, 'Urgencias')]
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


# --- cronograma -------------------------------------------------------------------------------
CRONOGRAMA = {}


def _fila_cronograma(cid):
    c = CRONOGRAMA[cid]
    equipo = next(e for e in INVENTARIO if e['id'] == c['id_equipo'])
    tecnico = dict(fake.personal()).get(c['tecnico_asignado_id'])
    return {'id': cid, 'fecha_programada': c['fecha_programada'], 'estado': c['estado'],
            'frecuencia_meses': c['frecuencia_meses'], 'observaciones': c['observaciones'],
            'id_equipo': c['id_equipo'], 'id_tipo': c['tipo_mantenimiento'], 'id_tecnico': c['tecnico_asignado_id'],
            'serie': equipo['serie'], 'inventario': equipo['inventario'], 'id_cliente': equipo.get('id_cliente', 3),
            'equipo': equipo['equipo'], 'marca': equipo['marca'], 'cliente': equipo['entidad'],
            'tipo': dict(fake.servicios()).get(c['tipo_mantenimiento']), 'tecnico': tecnico}


def _cronograma_entre(desde, hasta, hoy, estado=None, id_tecnico=None, id_cliente=None):
    filas = []
    for cid in sorted(CRONOGRAMA, key=lambda i: (CRONOGRAMA[i]['fecha_programada'], i)):
        f = _fila_cronograma(cid)
        if not desde <= f['fecha_programada'] <= hasta:
            continue
        if estado == 'vencido' and not (f['estado'] == 'pendiente' and f['fecha_programada'] < hoy):
            continue
        if estado and estado != 'vencido' and f['estado'] != estado:
            continue
        if (id_tecnico and f['id_tecnico'] != id_tecnico) or (id_cliente and f['id_cliente'] != id_cliente):
            continue
        filas.append(f)
    return filas


def _programar(filas):
    creados = 0
    for fila in filas:
        clave = (fila['id_equipo'], fila['tipo_mantenimiento'], fila['fecha_programada'])
        if any((c['id_equipo'], c['tipo_mantenimiento'], c['fecha_programada']) == clave for c in CRONOGRAMA.values()):
            continue
        CRONOGRAMA[max(CRONOGRAMA, default=0) + 1] = dict(fila, estado='pendiente')
        creados += 1
    return creados, len(filas) - creados


def _actualizar_cronograma(cid, estado, fecha, id_tecnico, observaciones):
    CRONOGRAMA[cid].update(estado=estado, fecha_programada=fecha, tecnico_asignado_id=id_tecnico,
                           observaciones=observaciones)
    return True


fake.equipos_cronograma = lambda: [
    {'id': e['id'], 'serie': e['serie'], 'inventario': e['inventario'], 'equipo': e['equipo'],
     'id_cliente': e.get('id_cliente', 3), 'cliente': e['entidad']} for e in INVENTARIO]
fake.cronograma_entre = _cronograma_entre
fake.obtener_cronograma = lambda cid: _fila_cronograma(cid) if cid in CRONOGRAMA else None
fake.programar_mantenimientos = _programar
fake.actualizar_cronograma = _actualizar_cronograma

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

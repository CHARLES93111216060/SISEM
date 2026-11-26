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
    for tabla in (DOCUMENTOS['factura'], DOCUMENTOS['orden'], CUENTAS, NOMINAS, METROLOGIA, REPORTES_METROLOGIA):
        tabla.clear()
    PERSONAL.clear()
    PERSONAL.update(_personal_inicial())


def guardar(reporte_id, equipo, datos, tecnico=1, cliente=7, fecha='2026-10-14'):
    REPORTES[reporte_id] = (reporte_id, tecnico, cliente, equipo, fecha, json.dumps(datos, ensure_ascii=False))


fake = types.ModuleType('database.db')
fake.personal = lambda: [(1, 'Juan Pérez'), (2, 'Ana Gómez')]    # se redefine abajo con PERSONAL
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

# --- personal y acceso (roles) -------------------------------------------------------------------
from werkzeug.security import generate_password_hash  # noqa: E402

CLAVE_FUNCIONARIO = 'Clave-funcionario-1'
_HASH_FUNCIONARIO = generate_password_hash(CLAVE_FUNCIONARIO)


def _personal_inicial():
    """Juan (id 1) tiene acceso activo como 'juan.perez'; Ana (id 2) no tiene acceso."""
    return {
        1: {'id': 1, 'nombre': 'Juan Pérez', 'documento': '1010', 'cargo': 'Técnico', 'profesion': 'Tecnólogo',
            'correo': 'juan@ejemplo.com', 'telefono': '3001234567', 'funcion': 'funcionario',
            'usuario': 'juan.perez', 'clave_hash': _HASH_FUNCIONARIO, 'acceso_activo': True, 'ultimo_acceso': None},
        2: {'id': 2, 'nombre': 'Ana Gómez', 'documento': '2020', 'cargo': 'Ingeniera', 'profesion': 'Ingeniera',
            'correo': '', 'telefono': None, 'funcion': 'funcionario',
            'usuario': None, 'clave_hash': None, 'acceso_activo': False, 'ultimo_acceso': None},
    }


PERSONAL = _personal_inicial()


class UsuarioDuplicado(Exception):
    pass


def _sin_clave(p):
    fila = {k: v for k, v in p.items() if k != 'clave_hash'}
    fila['tiene_clave'] = bool(p['clave_hash'])
    return fila


def _acceso_personal(usuario):
    for p in PERSONAL.values():
        if p['usuario'] and p['usuario'].lower() == usuario.lower():
            return {k: p[k] for k in ('id', 'nombre', 'funcion', 'clave_hash', 'acceso_activo')}
    return None


def _guardar_empleado(datos, empleado_id=None):
    if empleado_id is None:
        empleado_id = max(PERSONAL) + 1
        PERSONAL[empleado_id] = {'id': empleado_id, 'funcion': 'funcionario', 'usuario': None, 'clave_hash': None,
                                 'acceso_activo': False, 'ultimo_acceso': None}
    elif empleado_id not in PERSONAL:
        return None
    PERSONAL[empleado_id].update(datos)
    return empleado_id


def _configurar_acceso(empleado_id, usuario, clave_hash, activo):
    if empleado_id not in PERSONAL:
        return False
    if any(p['id'] != empleado_id and (p['usuario'] or '').lower() == usuario.lower() for p in PERSONAL.values()):
        raise UsuarioDuplicado()
    p = PERSONAL[empleado_id]
    p.update(usuario=usuario, acceso_activo=activo, clave_hash=clave_hash or p['clave_hash'])
    return True


fake.personal = lambda: [(p['id'], p['nombre']) for p in PERSONAL.values()]
fake.UsuarioDuplicado = UsuarioDuplicado
fake.acceso_personal = _acceso_personal
fake.estado_acceso = lambda pid: ({k: PERSONAL[pid][k] for k in ('funcion', 'clave_hash', 'acceso_activo')}
                                  if pid in PERSONAL else None)
fake.registrar_acceso = lambda pid: None
fake.empleados = lambda: [_sin_clave(p) for p in PERSONAL.values()]
fake.obtener_empleado = lambda eid: _sin_clave(PERSONAL[eid]) if eid in PERSONAL else None
fake.guardar_empleado = _guardar_empleado
fake.configurar_acceso = _configurar_acceso
fake.cambiar_clave = lambda eid, h: bool(PERSONAL.get(eid, {}).update(clave_hash=h) or eid in PERSONAL)

# --- facturas y órdenes de compra -----------------------------------------------------------------
DOCUMENTOS = {'factura': {}, 'orden': {}}


def _guardar_documento(tipo, datos, items):
    nuevo = max(DOCUMENTOS[tipo], default=0) + 1
    DOCUMENTOS[tipo][nuevo] = dict(datos, id=nuevo, estado='pendiente', items=items)
    return nuevo


def _obtener_documento(tipo, documento_id):
    d = DOCUMENTOS[tipo].get(documento_id)
    if not d:
        return None
    d = dict(d)
    if tipo == 'factura':
        d.update(cliente=dict(fake.clientes()).get(d['id_cliente']), nit='900.1', direccion='Calle 1',
                 telefono='601', correo='c@x.co', ciudad='Bogotá', departamento='Cundinamarca')
    return d


def _actualizar_estado_documento(tipo, documento_id, estado):
    if documento_id not in DOCUMENTOS[tipo]:
        return False
    DOCUMENTOS[tipo][documento_id]['estado'] = estado
    return True


fake.guardar_documento = _guardar_documento
fake.actualizar_estado_documento = _actualizar_estado_documento
fake.obtener_factura = lambda i: _obtener_documento('factura', i)
fake.obtener_orden = lambda i: _obtener_documento('orden', i)
fake.listar_facturas = lambda: [_obtener_documento('factura', i) for i in sorted(DOCUMENTOS['factura'], reverse=True)]
fake.listar_ordenes = lambda: [_obtener_documento('orden', i) for i in sorted(DOCUMENTOS['orden'], reverse=True)]

# --- cuentas de cobro y nómina --------------------------------------------------------------------
CUENTAS = {}
NOMINAS = {}


class NominaDuplicada(Exception):
    pass


def _con_empleado(registro):
    if not registro:
        return None
    p = PERSONAL.get(registro['id_personal'], {})
    return dict(registro, empleado=p.get('nombre'), documento=p.get('documento'), cargo=p.get('cargo'),
                correo=p.get('correo'), telefono=p.get('telefono'), profesion=p.get('profesion'))


def _guardar_cuenta(datos):
    nuevo = max(CUENTAS, default=0) + 1
    CUENTAS[nuevo] = dict(datos, id=nuevo, estado='pendiente')
    return nuevo


def _guardar_nomina(datos):
    if any(n['id_personal'] == datos['id_personal'] and n['periodo'] == datos['periodo'] for n in NOMINAS.values()):
        raise NominaDuplicada()
    nuevo = max(NOMINAS, default=0) + 1
    NOMINAS[nuevo] = dict(datos, id=nuevo)
    return nuevo


fake.guardar_cuenta_cobro = _guardar_cuenta
fake.listar_cuentas_cobro = lambda id_personal=None: [
    _con_empleado(c) for c in CUENTAS.values() if not id_personal or c['id_personal'] == id_personal]
fake.obtener_cuenta_cobro = lambda i: _con_empleado(CUENTAS.get(i))
fake.actualizar_estado_cuenta = lambda i, e, o: bool(i in CUENTAS and not CUENTAS[i].update(estado=e, observaciones=o))
fake.NominaDuplicada = NominaDuplicada
fake.guardar_nomina = _guardar_nomina
fake.listar_nomina = lambda id_personal=None: [
    _con_empleado(n) for n in NOMINAS.values() if not id_personal or n['id_personal'] == id_personal]
fake.obtener_nomina = lambda i: _con_empleado(NOMINAS.get(i))

# --- metrología -----------------------------------------------------------------------------------
METROLOGIA = {}
REPORTES_METROLOGIA = {}


class MetrologiaDuplicada(Exception):
    pass


def _fila_metrologia(mid):
    m = METROLOGIA[mid]
    equipo = next(e for e in INVENTARIO if e['id'] == m['id_equipo'])
    return dict(m, id=mid, serie=equipo['serie'], inventario=equipo['inventario'], equipo=equipo['equipo'],
                cliente=equipo['entidad'], responsable=dict(fake.personal()).get(m['id_responsable']))


def _programar_metrologia(datos):
    clave = (datos['id_equipo'], datos['tipo'], datos['fecha_programada'])
    if any((m['id_equipo'], m['tipo'], m['fecha_programada']) == clave for m in METROLOGIA.values()):
        raise MetrologiaDuplicada()
    nuevo = max(METROLOGIA, default=0) + 1
    METROLOGIA[nuevo] = dict(datos, estado='pendiente')
    return nuevo


def _guardar_reporte_metrologia(datos):
    nuevo = max(REPORTES_METROLOGIA, default=0) + 1
    REPORTES_METROLOGIA[nuevo] = dict(datos, id=nuevo)
    if datos['id_cronograma'] and METROLOGIA[datos['id_cronograma']]['id_equipo'] == datos['id_equipo']:
        METROLOGIA[datos['id_cronograma']]['estado'] = 'realizado'
    return nuevo


def _reporte_metrologia(rid):
    r = REPORTES_METROLOGIA.get(rid)
    if not r:
        return None
    equipo = next(e for e in INVENTARIO if e['id'] == r['id_equipo'])
    return dict(r, serie=equipo['serie'], inventario=equipo['inventario'], equipo=equipo['equipo'],
                marca=equipo['marca'], cliente=equipo['entidad'], tecnico=dict(fake.personal()).get(r['id_tecnico']),
                tecnico_cargo='Técnico')


fake.MetrologiaDuplicada = MetrologiaDuplicada
fake.programar_metrologia = _programar_metrologia
fake.metrologia_entre = lambda desde, hasta, estado=None: [
    _fila_metrologia(i) for i in sorted(METROLOGIA)
    if desde <= METROLOGIA[i]['fecha_programada'] <= hasta and (not estado or METROLOGIA[i]['estado'] == estado)]
fake.obtener_metrologia = lambda i: _fila_metrologia(i) if i in METROLOGIA else None
fake.metrologia_por_realizar = lambda: [
    _fila_metrologia(i) for i in METROLOGIA if METROLOGIA[i]['estado'] in ('pendiente', 'en_proceso')]
fake.actualizar_metrologia = lambda i, d: bool(i in METROLOGIA and not METROLOGIA[i].update(d))
fake.guardar_reporte_metrologia = _guardar_reporte_metrologia
fake.listar_reportes_metrologia = lambda: [_reporte_metrologia(i) for i in sorted(REPORTES_METROLOGIA, reverse=True)]
fake.obtener_reporte_metrologia = _reporte_metrologia
fake.resumen_pendientes = lambda id_personal=None: {
    'facturas': sum(d['estado'] == 'pendiente' for d in DOCUMENTOS['factura'].values()),
    'ordenes': len(DOCUMENTOS['orden']), 'metrologia_vencida': 0,
    'cuentas': sum(c['estado'] == 'pendiente' and (not id_personal or c['id_personal'] == id_personal)
                   for c in CUENTAS.values())}

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

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

"""Pruebas de roles (admin / funcionario) y de los módulos de Comercial, Talento humano,
Laboratorio y Mi cuenta. Usan la base simulada de tests/helpers.py (sin PostgreSQL ni WeasyPrint)."""
import re
import unittest
from datetime import date, timedelta
from decimal import Decimal

from tests import helpers
from tests.helpers import CLAVE_FUNCIONARIO, informes, sisem

app = helpers.aplicacion
TOKEN = 'token-de-prueba'


def cliente(rol='admin', personal_id=1):
    """Cliente con sesión del rol pedido. El funcionario lleva la huella de su clave real."""
    c = sisem.test_client()
    with c.session_transaction() as s:
        s['_csrf'] = TOKEN
        if rol == 'admin':
            s.update(username='admin', rol='admin', nombre='admin')
        elif rol == 'funcionario':
            p = helpers.PERSONAL[personal_id]
            s.update(username=p['usuario'], rol='funcionario', nombre=p['nombre'], personal_id=personal_id,
                     huella=app._huella(p['clave_hash']))
    return c


def post(c, ruta, datos=None):
    return c.post(ruta, data=dict(datos or {}, csrf_token=TOKEN))


class _Base(unittest.TestCase):
    def setUp(self):
        helpers.reiniciar()
        app._fallos.clear()
        self.html_pdf = []
        self._original = informes._html_a_pdf
        informes._html_a_pdf = lambda html: (self.html_pdf.append(html), b'%PDF-prueba')[1]

    def tearDown(self):
        informes._html_a_pdf = self._original


# ---------------------------------------------------------------------------------------------
class Roles(_Base):
    def test_login_de_funcionario(self):
        c = cliente(None)
        r = post(c, '/login', {'username': 'JUAN.PEREZ', 'password': CLAVE_FUNCIONARIO})   # sin distinguir mayúsculas
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers['Location'].endswith('/dashboard'))
        with c.session_transaction() as s:
            self.assertEqual((s['rol'], s['personal_id'], s['nombre']), ('funcionario', 1, 'Juan Pérez'))
        self.assertIn('Funcionario', c.get('/dashboard').get_data(as_text=True))

    def test_login_rechazado(self):
        c = cliente(None)
        self.assertEqual(post(c, '/login', {'username': 'juan.perez', 'password': 'otra-clave-123'}).status_code, 401)
        self.assertEqual(post(c, '/login', {'username': 'nadie', 'password': CLAVE_FUNCIONARIO}).status_code, 401)
        # acceso inactivo o función distinta de "funcionario": no entra aunque la clave sea correcta
        helpers.PERSONAL[1]['acceso_activo'] = False
        self.assertEqual(post(c, '/login', {'username': 'juan.perez', 'password': CLAVE_FUNCIONARIO}).status_code, 401)
        helpers.PERSONAL[1].update(acceso_activo=True, funcion='contratista')
        self.assertEqual(post(c, '/login', {'username': 'juan.perez', 'password': CLAVE_FUNCIONARIO}).status_code, 401)

    def test_login_de_admin_sigue_igual(self):
        r = post(cliente(None), '/login', {'username': 'admin', 'password': 'Contraseña-de-prueba-1'})
        self.assertEqual(r.status_code, 302)

    def test_funcionario_no_entra_a_rutas_de_admin(self):
        c = cliente('funcionario')
        for ruta in ('/comercial/facturacion', '/comercial/facturacion/nueva', '/comercial/ordenes',
                     '/talento_humano/empleados', '/talento_humano/roles', '/talento_humano/roles/2',
                     '/talento_humano/nomina/nueva', '/comercial/ingresar_inventario', '/comercial/cargar_excel'):
            self.assertEqual(c.get(ruta).status_code, 403, ruta)
        for ruta in ('/comercial/facturacion/guardar', '/comercial/guardar_inventario', '/talento_humano/nomina/guardar',
                     '/talento_humano/empleados/guardar', '/talento_humano/roles/2/guardar',
                     '/talento_humano/cuentas_cobro/1/estado'):
            self.assertEqual(post(c, ruta).status_code, 403, ruta)

    def test_funcionario_si_usa_sus_modulos(self):
        c = cliente('funcionario')
        for ruta in ('/dashboard', '/clientes', '/mantenimiento/reportes', '/mantenimiento/historial',
                     '/comercial/inventario', '/talento_humano/cuentas_cobro', '/talento_humano/nomina',
                     '/laboratorio/cronograma_metrologia', '/laboratorio/reportes_metrologia',
                     '/laboratorio/reportes_metrologia/nuevo', '/cuenta'):
            self.assertEqual(c.get(ruta).status_code, 200, ruta)

    def test_menu_segun_el_rol(self):
        html = cliente('funcionario').get('/dashboard').get_data(as_text=True)
        self.assertNotIn('/comercial/facturacion', html)
        self.assertNotIn('/talento_humano/roles', html)
        self.assertIn('Mis cuentas de cobro', html)
        html = cliente('admin').get('/dashboard').get_data(as_text=True)
        self.assertIn('/comercial/facturacion', html)
        self.assertIn('/talento_humano/roles', html)

    def test_sesion_cae_si_se_desactiva_el_acceso_o_cambia_la_clave(self):
        c = cliente('funcionario')
        self.assertEqual(c.get('/dashboard').status_code, 200)
        helpers.PERSONAL[1]['acceso_activo'] = False
        self.assertEqual(c.get('/dashboard').status_code, 302)

        c = cliente('funcionario')
        helpers.PERSONAL[1]['acceso_activo'] = True
        helpers.PERSONAL[1]['clave_hash'] = app.generate_password_hash('Otra-clave-larga-1')
        self.assertEqual(c.get('/dashboard').status_code, 302)

    def test_sesion_sin_rol_no_sirve(self):
        c = sisem.test_client()
        with c.session_transaction() as s:
            s['username'] = 'admin'                    # sesión antigua sin rol
        self.assertEqual(c.get('/dashboard').status_code, 302)


# ---------------------------------------------------------------------------------------------
class EmpleadosYAcceso(_Base):
    def test_crear_y_editar_empleado(self):
        c = cliente()
        datos = {'nombre': 'Laura <b>Ruiz</b>', 'documento': '52.123.456', 'cargo': 'Técnica',
                 'profesion': 'Tecnóloga', 'correo': 'laura@ejemplo.com', 'telefono': '300 123 4567'}
        r = post(c, '/talento_humano/empleados/guardar', datos)
        self.assertEqual(r.status_code, 302)
        nuevo = max(helpers.PERSONAL)
        self.assertEqual(helpers.PERSONAL[nuevo]['funcion'], 'funcionario')
        self.assertFalse(helpers.PERSONAL[nuevo]['acceso_activo'])
        html = c.get('/talento_humano/empleados').get_data(as_text=True)
        self.assertIn('Laura &lt;b&gt;Ruiz&lt;/b&gt;', html)                 # escapado

        r = post(c, '/talento_humano/empleados/guardar', dict(datos, id=str(nuevo), cargo='Coordinadora'))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(helpers.PERSONAL[nuevo]['cargo'], 'Coordinadora')
        self.assertEqual(c.get(f'/talento_humano/empleados/{nuevo}/editar').status_code, 200)

    def test_empleado_invalido(self):
        c = cliente()
        base = {'nombre': 'X', 'documento': '1234', 'cargo': 'C', 'profesion': 'P'}
        self.assertEqual(post(c, '/talento_humano/empleados/guardar', dict(base, correo='no-es-correo')).status_code, 400)
        self.assertEqual(post(c, '/talento_humano/empleados/guardar', dict(base, documento='12')).status_code, 400)
        self.assertEqual(post(c, '/talento_humano/empleados/guardar', dict(base, nombre='')).status_code, 400)
        self.assertEqual(post(c, '/talento_humano/empleados/guardar', dict(base, id='999')).status_code, 404)

    def test_configurar_acceso(self):
        c = cliente()
        ruta = '/talento_humano/roles/2/guardar'
        clave = 'Clave-de-Ana-123'
        # activar sin contraseña, usuario reservado, repetido, clave corta o distinta: rechazado
        self.assertEqual(post(c, ruta, {'usuario': 'ana', 'acceso_activo': 'si'}).status_code, 400)
        self.assertEqual(post(c, ruta, {'usuario': 'ADMIN', 'clave': clave, 'confirmar': clave}).status_code, 400)
        self.assertEqual(post(c, ruta, {'usuario': 'Juan.Perez', 'clave': clave, 'confirmar': clave}).status_code, 409)
        self.assertEqual(post(c, ruta, {'usuario': 'ana', 'clave': 'corta', 'confirmar': 'corta'}).status_code, 400)
        self.assertEqual(post(c, ruta, {'usuario': 'ana', 'clave': clave, 'confirmar': clave + 'x'}).status_code, 400)
        self.assertEqual(post(c, ruta, {'usuario': 'a b', 'clave': clave, 'confirmar': clave}).status_code, 400)

        r = post(c, ruta, {'usuario': 'ana', 'clave': clave, 'confirmar': clave, 'acceso_activo': 'si'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(helpers.PERSONAL[2]['acceso_activo'])
        self.assertNotEqual(helpers.PERSONAL[2]['clave_hash'], clave)              # se guarda el hash
        self.assertEqual(post(cliente(None), '/login', {'username': 'ana', 'password': clave}).status_code, 302)

        html = c.get('/talento_humano/roles').get_data(as_text=True)
        self.assertNotIn('scrypt', html)                                            # el hash nunca sale

    def test_cambiar_mi_clave(self):
        c = cliente('funcionario')
        nueva = 'Mi-clave-nueva-2026'
        self.assertEqual(post(c, '/cuenta/clave', {'actual': 'mala', 'nueva': nueva, 'confirmar': nueva}).status_code, 400)
        self.assertEqual(post(c, '/cuenta/clave', {'actual': CLAVE_FUNCIONARIO, 'nueva': 'corta',
                                                   'confirmar': 'corta'}).status_code, 400)
        r = post(c, '/cuenta/clave', {'actual': CLAVE_FUNCIONARIO, 'nueva': nueva, 'confirmar': nueva})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.get('/dashboard').status_code, 200)                     # esta sesión sigue viva
        self.assertEqual(post(cliente(None), '/login', {'username': 'juan.perez', 'password': nueva}).status_code, 302)

    def test_bloqueo_de_clave_actual(self):
        c = cliente('funcionario')
        for _ in range(app.MAX_INTENTOS):
            post(c, '/cuenta/clave', {'actual': 'mala', 'nueva': 'x' * 12, 'confirmar': 'x' * 12})
        r = post(c, '/cuenta/clave', {'actual': CLAVE_FUNCIONARIO, 'nueva': 'x' * 12, 'confirmar': 'x' * 12})
        self.assertEqual(r.status_code, 429)

    def test_admin_no_cambia_clave_desde_la_web(self):
        self.assertEqual(post(cliente(), '/cuenta/clave', {}).status_code, 403)


# ---------------------------------------------------------------------------------------------
class Facturacion(_Base):
    def _factura(self, **cambios):
        datos = {'cliente': '7', 'fecha_emision': '2026-11-01', 'fecha_vencimiento': '2026-11-30',
                 'item_descripcion': ['Mantenimiento preventivo', 'Repuesto <x>', ''],
                 'item_cantidad': ['2', '1', ''], 'item_valor': ['150000', '20000.50', ''],
                 'item_iva': ['19', '0', '0'], 'observaciones': 'Pago a 30 días', 'total': '1'}
        datos.update(cambios)
        return datos

    def test_crear_factura_calcula_totales_en_el_servidor(self):
        c = cliente()
        r = post(c, '/comercial/facturacion/guardar', self._factura())
        self.assertEqual(r.status_code, 302)
        f = helpers.DOCUMENTOS['factura'][1]
        self.assertEqual(f['subtotal'], Decimal('320000.50'))
        self.assertEqual(f['iva'], Decimal('57000.00'))
        self.assertEqual(f['total'], Decimal('377000.50'))                         # el "total" enviado se ignora
        self.assertEqual(len(f['items']), 2)
        self.assertEqual(f['creado_por'], 'admin')
        html = c.get('/comercial/facturacion/1').get_data(as_text=True)
        self.assertIn('FAC-000001', html)
        self.assertIn('$ 377.000,50', html)
        self.assertIn('Repuesto &lt;x&gt;', html)

    def test_factura_invalida(self):
        c = cliente()
        casos = [
            {'fecha_vencimiento': '2026-10-01'},                                   # vence antes de emitir
            {'cliente': '999'},
            {'item_descripcion': ['', ''], 'item_cantidad': ['', ''], 'item_valor': ['', '']},   # sin ítems
            {'item_cantidad': ['0', '1']},
            {'item_valor': ['1.500.000', '1']},                                   # puntos de miles
            {'item_iva': ['16', '0']},                                            # tarifa no permitida
            {'item_descripcion': ['x'] * 11, 'item_cantidad': ['1'] * 11, 'item_valor': ['1'] * 11},
        ]
        for cambio in casos:
            r = post(c, '/comercial/facturacion/guardar', self._factura(**cambio))
            self.assertEqual(r.status_code, 400, cambio)
        self.assertFalse(helpers.DOCUMENTOS['factura'])

    def test_estado_y_pdf(self):
        c = cliente()
        post(c, '/comercial/facturacion/guardar', self._factura())
        self.assertEqual(post(c, '/comercial/facturacion/1/estado', {'estado': 'pagada'}).status_code, 302)
        self.assertEqual(helpers.DOCUMENTOS['factura'][1]['estado'], 'pagada')
        self.assertEqual(post(c, '/comercial/facturacion/1/estado', {'estado': 'borrada'}).status_code, 400)
        self.assertEqual(post(c, '/comercial/facturacion/9/estado', {'estado': 'pagada'}).status_code, 404)

        r = c.get('/comercial/facturacion/1/descargar')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, 'application/pdf')
        self.assertIn('FAC-000001', r.headers['Content-Disposition'])
        html = self.html_pdf[-1]
        self.assertIn('FACTURA DE VENTA', html)
        self.assertIn('no reemplaza la factura electrónica DIAN', html)
        self.assertIn('Repuesto &lt;x&gt;', html)
        self.assertEqual(c.get('/comercial/facturacion/9/descargar').status_code, 404)

    def test_orden_de_compra(self):
        c = cliente()
        datos = {'proveedor': 'Repuestos S.A.S.', 'nit_proveedor': '900.123', 'fecha': '2026-11-10',
                 'fecha_entrega': '2026-11-05', 'item_descripcion': ['Batería'], 'item_cantidad': ['3'],
                 'item_valor': ['85000'], 'item_iva': ['19']}
        self.assertEqual(post(c, '/comercial/ordenes/guardar', datos).status_code, 400)   # entrega antes de la orden
        datos['fecha_entrega'] = '2026-11-20'
        self.assertEqual(post(c, '/comercial/ordenes/guardar', datos).status_code, 302)
        self.assertEqual(helpers.DOCUMENTOS['orden'][1]['total'], Decimal('303450.00'))
        self.assertIn('OC-000001', c.get('/comercial/ordenes').get_data(as_text=True))
        self.assertEqual(c.get('/comercial/ordenes/1/descargar').status_code, 200)
        self.assertIn('ORDEN DE COMPRA', self.html_pdf[-1])


# ---------------------------------------------------------------------------------------------
class CuentasDeCobro(_Base):
    DATOS = {'fecha': '2026-11-25', 'periodo_inicio': '2026-11-01', 'periodo_fin': '2026-11-30',
             'concepto': 'Servicios de mantenimiento', 'valor': '1800000'}

    def test_funcionario_radica_solo_a_su_nombre(self):
        c = cliente('funcionario')
        r = post(c, '/talento_humano/cuentas_cobro/guardar', dict(self.DATOS, empleado='2'))   # intenta a nombre de Ana
        self.assertEqual(r.status_code, 302)
        self.assertEqual(helpers.CUENTAS[1]['id_personal'], 1)

    def test_funcionario_no_ve_cuentas_ajenas(self):
        post(cliente(), '/talento_humano/cuentas_cobro/guardar', dict(self.DATOS, empleado='2'))   # de Ana
        c = cliente('funcionario')                                                                 # Juan
        self.assertEqual(c.get('/talento_humano/cuentas_cobro/1').status_code, 404)
        self.assertEqual(c.get('/talento_humano/cuentas_cobro/1/descargar').status_code, 404)
        self.assertNotIn('CC-000001', c.get('/talento_humano/cuentas_cobro').get_data(as_text=True))
        self.assertIn('CC-000001', cliente().get('/talento_humano/cuentas_cobro').get_data(as_text=True))

    def test_validaciones_y_gestion(self):
        c = cliente()
        self.assertEqual(post(c, '/talento_humano/cuentas_cobro/guardar', dict(self.DATOS)).status_code, 400)  # sin empleado
        for cambio in ({'valor': '0'}, {'valor': '-5'}, {'periodo_fin': '2026-10-01'}, {'concepto': ''}):
            datos = dict(self.DATOS, empleado='1', **cambio)
            self.assertEqual(post(c, '/talento_humano/cuentas_cobro/guardar', datos).status_code, 400, cambio)
        post(c, '/talento_humano/cuentas_cobro/guardar', dict(self.DATOS, empleado='1'))
        r = post(c, '/talento_humano/cuentas_cobro/1/estado', {'estado': 'aprobada', 'observaciones': 'OK'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(helpers.CUENTAS[1]['estado'], 'aprobada')
        self.assertEqual(c.get('/talento_humano/cuentas_cobro/1/descargar').status_code, 200)
        self.assertIn('CUENTA DE COBRO', self.html_pdf[-1])
        self.assertIn('$ 1.800.000,00', self.html_pdf[-1])


# ---------------------------------------------------------------------------------------------
class Nomina(_Base):
    DATOS = {'empleado': '1', 'periodo': '2026-11', 'salario_base': '2000000', 'dias_trabajados': '30',
             'auxilio_transporte': '200000', 'horas_extra': '100000', 'bonificaciones': '50000',
             'otras_deducciones': '10000'}

    def test_calculo(self):
        r = app.liquidar_nomina(Decimal('2000000'), 15, Decimal('200000'), Decimal('0'), Decimal('0'), Decimal('0'))
        self.assertEqual(r['salario_periodo'], Decimal('1000000'))
        self.assertEqual(r['auxilio_periodo'], Decimal('100000'))
        self.assertEqual((r['salud'], r['pension']), (Decimal('40000'), Decimal('40000')))
        self.assertEqual(r['neto_pagar'], Decimal('1020000'))

    def test_liquidar_y_duplicado(self):
        c = cliente()
        self.assertEqual(post(c, '/talento_humano/nomina/guardar', self.DATOS).status_code, 302)
        n = helpers.NOMINAS[1]
        self.assertEqual(n['periodo'], date(2026, 11, 1))
        self.assertEqual(n['total_devengado'], Decimal('2350000'))
        self.assertEqual(n['total_deducciones'], Decimal('178000'))                 # 84000 + 84000 + 10000
        self.assertEqual(n['neto_pagar'], Decimal('2172000'))
        self.assertEqual(post(c, '/talento_humano/nomina/guardar', self.DATOS).status_code, 409)

    def test_nomina_invalida(self):
        c = cliente()
        for cambio in ({'periodo': '2026-13'}, {'dias_trabajados': '31'}, {'salario_base': 'mucho'},
                       {'otras_deducciones': '99999999'}, {'empleado': '77'}):
            self.assertEqual(post(c, '/talento_humano/nomina/guardar', dict(self.DATOS, **cambio)).status_code, 400, cambio)

    def test_funcionario_ve_solo_su_desprendible(self):
        c = cliente()
        post(c, '/talento_humano/nomina/guardar', self.DATOS)                         # de Juan
        post(c, '/talento_humano/nomina/guardar', dict(self.DATOS, empleado='2'))     # de Ana
        juan = cliente('funcionario')
        html = juan.get('/talento_humano/nomina').get_data(as_text=True)
        self.assertIn('/talento_humano/nomina/1', html)
        self.assertNotIn('/talento_humano/nomina/2', html)
        self.assertEqual(juan.get('/talento_humano/nomina/2').status_code, 404)
        self.assertEqual(juan.get('/talento_humano/nomina/1/descargar').status_code, 200)
        self.assertIn('DESPRENDIBLE DE NÓMINA', self.html_pdf[-1])
        self.assertIn('Noviembre de 2026', self.html_pdf[-1])


# ---------------------------------------------------------------------------------------------
class Metrologia(_Base):
    def _programar(self, c, **cambios):
        datos = {'equipo': '4', 'tipo': 'calibracion', 'fecha_programada': '2026-12-10', 'responsable': '1'}
        datos.update(cambios)
        return post(c, '/laboratorio/cronograma_metrologia/guardar', datos)

    def test_calculo_de_mediciones(self):
        mediciones, resultado = app.calcular_mediciones([('120', '121,5', '2'), ('', '', ''), ('80', '83', '2')])
        self.assertEqual(len(mediciones), 2)
        self.assertEqual(mediciones[0]['error'], '1.5')
        self.assertTrue(mediciones[0]['cumple'])
        self.assertFalse(mediciones[1]['cumple'])
        self.assertEqual(resultado, 'no_conforme')
        self.assertEqual(app.calcular_mediciones([('-5', '-5.1', '0.2')])[1], 'conforme')
        with self.assertRaises(ValueError):
            app.calcular_mediciones([('1', 'abc', '1')])
        with self.assertRaises(ValueError):
            app.calcular_mediciones([('1', '1', '-1')])
        with self.assertRaises(ValueError):
            app.calcular_mediciones([('', '', '')])

    def test_programar_y_cronograma(self):
        c = cliente('funcionario')
        self.assertEqual(self._programar(c).status_code, 302)
        self.assertEqual(self._programar(c).status_code, 409)                          # duplicado
        self.assertEqual(self._programar(c, tipo='otra').status_code, 400)
        self.assertEqual(self._programar(c, equipo='999').status_code, 400)
        html = c.get('/laboratorio/cronograma_metrologia?anio=2026').get_data(as_text=True)
        self.assertIn('Diciembre', html)
        self.assertIn('SN-77', html)
        self.assertEqual(c.get('/laboratorio/cronograma_metrologia?anio=abc').status_code, 200)

    def test_vencido_y_actualizar(self):
        c = cliente()
        ayer = (date.today() - timedelta(days=1)).isoformat()
        self._programar(c, fecha_programada=ayer)
        self.assertIn('Vencido', c.get('/laboratorio/cronograma_metrologia/1').get_data(as_text=True))
        r = post(c, '/laboratorio/cronograma_metrologia/1/actualizar',
                 {'estado': 'en_proceso', 'fecha_programada': ayer, 'proveedor': 'Lab ABC'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(helpers.METROLOGIA[1]['estado'], 'en_proceso')
        self.assertEqual(post(c, '/laboratorio/cronograma_metrologia/1/actualizar',
                              {'estado': 'x', 'fecha_programada': ayer}).status_code, 400)

    def _reporte(self, **cambios):
        datos = {'equipo': '4', 'tipo': 'calibracion', 'fecha': '2026-12-10', 'tecnico': '1',
                 'patron': 'Analizador <Fluke>', 'magnitud': 'Flujo', 'unidad': 'L/min',
                 'med_patron': ['1', '5'], 'med_medido': ['1.05', '5.1'], 'med_tolerancia': ['0.1', '0.2']}
        datos.update(cambios)
        return datos

    def test_reporte_enlazado_marca_realizado_y_pdf(self):
        c = cliente('funcionario')
        self._programar(c)
        self.assertIn('value="4" selected', c.get('/laboratorio/reportes_metrologia/nuevo?programacion=1')
                      .get_data(as_text=True))                                           # equipo precargado
        r = post(c, '/laboratorio/reportes_metrologia/guardar', self._reporte(programacion='1', resultado='no_conforme'))
        self.assertEqual(r.status_code, 302)
        reporte = helpers.REPORTES_METROLOGIA[1]
        self.assertEqual(reporte['resultado'], 'conforme')                              # lo calcula el servidor
        self.assertEqual(helpers.METROLOGIA[1]['estado'], 'realizado')
        self.assertEqual(c.get('/laboratorio/reportes_metrologia/1/descargar').status_code, 200)
        html = self.html_pdf[-1]
        self.assertIn('REPORTE DE CALIBRACIÓN', html)
        self.assertIn('Analizador &lt;Fluke&gt;', html)
        self.assertIn('CONFORME', html)

    def test_reporte_invalido(self):
        c = cliente()
        self._programar(c)
        casos = [{'programacion': '1', 'equipo': '9'},                                  # actividad de otro equipo
                 {'med_patron': [], 'med_medido': [], 'med_tolerancia': []},
                 {'proxima_fecha': '2026-12-01'},                                       # antes de la fecha
                 {'tecnico': '50'}, {'patron': ''}]
        for cambio in casos:
            r = post(c, '/laboratorio/reportes_metrologia/guardar', self._reporte(**cambio))
            self.assertEqual(r.status_code, 400, cambio)
        self.assertFalse(helpers.REPORTES_METROLOGIA)

    def test_rutas_antiguas_del_menu(self):
        c = cliente()
        self.assertTrue(c.get('/laboratorio/hojas_de_vida').headers['Location'].endswith('/consultar_hv'))
        self.assertTrue(c.get('/talento_humano/asistencia').headers['Location'].endswith('/talento_humano/cuentas_cobro'))
        self.assertTrue(c.get('/comercial/clientes').headers['Location'].endswith('/comercial/facturacion'))


# ---------------------------------------------------------------------------------------------
class SeguridadModulos(_Base):
    PAGINAS = ('/comercial/facturacion', '/comercial/facturacion/nueva', '/comercial/ordenes/nueva',
               '/talento_humano/empleados', '/talento_humano/empleados/nuevo', '/talento_humano/roles',
               '/talento_humano/roles/1', '/talento_humano/cuentas_cobro/nueva', '/talento_humano/nomina/nueva',
               '/laboratorio/cronograma_metrologia', '/laboratorio/cronograma_metrologia/nuevo',
               '/laboratorio/reportes_metrologia/nuevo', '/cuenta')

    def test_paginas_cargan_y_sin_scripts_inline(self):
        c = cliente()
        for ruta in self.PAGINAS:
            r = c.get(ruta)
            self.assertEqual(r.status_code, 200, ruta)
            for m in re.finditer(r'<script(?![^>]*\bsrc=)[^>]*>', r.get_data(as_text=True)):
                self.fail(f'script inline en {ruta}: {m.group(0)}')

    def test_post_sin_csrf_se_rechaza(self):
        c = cliente()
        for ruta in ('/comercial/facturacion/guardar', '/talento_humano/roles/1/guardar',
                     '/talento_humano/nomina/guardar', '/laboratorio/reportes_metrologia/guardar'):
            self.assertEqual(c.post(ruta, data={}).status_code, 400, ruta)

    def test_sin_sesion_redirige(self):
        c = sisem.test_client()
        for ruta in self.PAGINAS:
            self.assertEqual(c.get(ruta).status_code, 302, ruta)


if __name__ == '__main__':
    unittest.main()

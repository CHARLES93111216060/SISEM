import io
import json
import re
import unittest

from tests import helpers
from tests.helpers import DATOS_COMUNES, informes, sisem

USUARIO, CLAVE = 'admin', 'Contraseña-de-prueba-1'


def cliente_sesion(autenticado=True):
    c = sisem.test_client()
    with c.session_transaction() as s:
        s['_csrf'] = 'token-de-prueba'
        if autenticado:
            s['username'] = USUARIO
    return c


CSRF = {'X-CSRF-Token': 'token-de-prueba'}


class Seguridad(unittest.TestCase):
    def setUp(self):
        helpers.reiniciar()
        helpers.aplicacion._fallos.clear()

    def test_rutas_exigen_login(self):
        c = sisem.test_client()
        for ruta in ('/dashboard', '/clientes', '/mantenimiento/reportes', '/mantenimiento/historial',
                     '/mantenimiento/reportes/1/descargar', '/talento_humano/nomina'):
            r = c.get(ruta)
            self.assertEqual(r.status_code, 302, ruta)
            self.assertIn('/login', r.headers['Location'])
        # las rutas que consume el JavaScript responden 401 en JSON (no una redirección HTML)
        self.assertEqual(c.get('/load_form/2').status_code, 401)

    def test_ajax_sin_sesion_devuelve_401_json(self):
        r = sisem.test_client().get('/load_form/2', headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertEqual(r.status_code, 401)

    def test_login_correcto_y_incorrecto(self):
        c = cliente_sesion(False)
        malo = c.post('/login', data={'username': USUARIO, 'password': 'otra', 'csrf_token': 'token-de-prueba'})
        self.assertEqual(malo.status_code, 401)
        bueno = c.post('/login', data={'username': USUARIO, 'password': CLAVE, 'csrf_token': 'token-de-prueba'})
        self.assertEqual(bueno.status_code, 302)
        self.assertTrue(bueno.headers['Location'].endswith('/dashboard'))

    def test_login_sin_csrf_se_rechaza(self):
        r = sisem.test_client().post('/login', data={'username': USUARIO, 'password': CLAVE})
        self.assertEqual(r.status_code, 400)

    def test_bloqueo_tras_intentos_fallidos(self):
        c = cliente_sesion(False)
        for _ in range(helpers.aplicacion.MAX_INTENTOS):
            c.post('/login', data={'username': USUARIO, 'password': 'x', 'csrf_token': 'token-de-prueba'})
        r = c.post('/login', data={'username': USUARIO, 'password': CLAVE, 'csrf_token': 'token-de-prueba'})
        self.assertEqual(r.status_code, 429)

    def test_logout_solo_post_y_con_csrf(self):
        c = cliente_sesion()
        self.assertEqual(c.get('/logout').status_code, 405)
        self.assertEqual(c.post('/logout').status_code, 400)
        r = c.post('/logout', data={'csrf_token': 'token-de-prueba'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.get('/dashboard').status_code, 302)

    def test_cabeceras_de_seguridad(self):
        r = cliente_sesion().get('/dashboard')
        self.assertEqual(r.status_code, 200)
        self.assertIn("script-src 'self'", r.headers['Content-Security-Policy'])
        self.assertEqual(r.headers['X-Frame-Options'], 'DENY')
        self.assertEqual(r.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(r.headers['Cache-Control'], 'no-store')
        # los estáticos sí pueden cachearse
        self.assertNotEqual(sisem.test_client().get('/static/css/style.css').headers.get('Cache-Control'), 'no-store')

    def test_cookie_de_sesion(self):
        r = sisem.test_client().get('/login')
        cookie = r.headers.get('Set-Cookie', '')
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)

    def test_sin_scripts_inline_en_las_paginas(self):
        c = cliente_sesion()
        for ruta in ('/dashboard', '/clientes', '/mantenimiento/reportes', '/mantenimiento/historial'):
            html = c.get(ruta).get_data(as_text=True)
            for m in re.finditer(r'<script(?![^>]*\bsrc=)[^>]*>', html):
                self.fail(f'script inline en {ruta}: {m.group(0)}')

    def test_modulo_pendiente_y_404(self):
        c = cliente_sesion()
        self.assertEqual(c.get('/talento_humano/nomina').status_code, 200)
        self.assertEqual(c.get('/cosa/inexistente').status_code, 404)
        self.assertEqual(c.get('/static/css/style.css').status_code, 200)

    def test_errores_no_filtran_detalles(self):
        c = cliente_sesion()
        original = helpers.fake.subir_reporte
        helpers.fake.subir_reporte = lambda *a: (_ for _ in ()).throw(RuntimeError('password=SECRETO host=10.0.0.5'))
        try:
            datos = dict(DATOS_COMUNES, **{'equipo.tipo': '2'})
            r = c.post('/mantenimiento/reportes/guardar_informe', json=datos, headers=CSRF)
        finally:
            helpers.fake.subir_reporte = original
        self.assertEqual(r.status_code, 500)
        self.assertNotIn('SECRETO', r.get_data(as_text=True))


class GuardarInforme(unittest.TestCase):
    def setUp(self):
        helpers.reiniciar()
        self.c = cliente_sesion()

    def post(self, datos, headers=CSRF):
        return self.c.post('/mantenimiento/reportes/guardar_informe', json=datos, headers=headers)

    def valido(self, **extra):
        datos = dict(DATOS_COMUNES, **{'equipo.tipo': '2'})
        datos.update(extra)
        return datos

    def test_guarda_y_devuelve_id(self):
        r = self.post(self.valido())
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['id'], 1)
        self.assertEqual(json.loads(helpers.REPORTES[1][5])['equipo.modelo'], 'Infusomat')

    def test_sin_csrf(self):
        self.assertEqual(self.post(self.valido(), headers={}).status_code, 400)

    def test_validaciones(self):
        casos = {
            'tipo inexistente': self.valido(**{'equipo.tipo': '99'}),
            'tipo no numérico': self.valido(**{'equipo.tipo': '2 OR 1=1'}),
            'fecha inválida': self.valido(**{'general.fecha_mantenimiento': '2026-13-45'}),
            'fecha con formato raro': self.valido(**{'general.fecha_mantenimiento': '14/10/2026'}),
            'falta técnico': {k: v for k, v in self.valido().items() if k != 'tecnico.nombre'},
            'valor anidado': self.valido(**{'equipo.extra': {'a': 1}}),
            'valor numérico': self.valido(**{'equipo.modelo': 5}),
            'texto enorme': self.valido(**{'servicio.actividad': 'x' * 6000}),
        }
        for nombre, datos in casos.items():
            with self.subTest(nombre):
                self.assertEqual(self.post(datos).status_code, 400)
        self.assertEqual(helpers.REPORTES, {})

    def test_cuerpo_no_json(self):
        r = self.c.post('/mantenimiento/reportes/guardar_informe', data='hola', headers=CSRF)
        self.assertEqual(r.status_code, 400)

    def test_cuerpo_demasiado_grande(self):
        r = self.c.post('/mantenimiento/reportes/guardar_informe', data='x' * (2 * 1024 * 1024),
                        headers=dict(CSRF, **{'Content-Type': 'application/json'}))
        self.assertEqual(r.status_code, 413)


class ContenidoDelPDF(unittest.TestCase):
    """Verifica contexto y HTML de cada informe (sin necesidad de WeasyPrint)."""

    def setUp(self):
        helpers.reiniciar()
        self.ctx = sisem.test_request_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)

    def html(self, tipo, extra):
        helpers.guardar(1, tipo, dict(DATOS_COMUNES, **{'equipo.tipo': str(tipo)}, **extra))
        return informes.generar_reporte_html(1)

    def test_bomba_calculos(self):
        extra = {'protocolo_infusion.pantalla_botones': 'Cumple', 'protocolo_infusion.alarma_aire': 'No cumple',
                 'protocolo_infusion.volumen_medio': '19,6', 'protocolo_infusion.tiempo_medio': '20.4'}
        helpers.guardar(1, 2, dict(DATOS_COMUNES, **{'equipo.tipo': '2'}, **extra))
        _, ctx = informes.construir_contexto(1)
        self.assertEqual(ctx['med']['flujo'], '57.6')
        self.assertEqual(ctx['med']['pct_volumen'], '2.0')
        self.assertEqual(ctx['cumple'], 'Sí')
        self.assertTrue(any(a['incumple'] for a in ctx['alarmas']))
        html = informes.generar_reporte_html(1)
        for esperado in ('Infusomat', 'SN-1', 'INV-9', 'Clínica &lt;Norte&gt; &amp; Cía', 'B. Braun',
                         'Juan Pérez', '14/10/2026', 'UCI intermédios', 'PM-020', 'Funciona correctamente'):
            self.assertIn(esperado, html)
        self.assertNotIn('<<', html)

    def test_bomba_fuera_de_tolerancia(self):
        extra = {'protocolo_infusion.volumen_medio': '15', 'protocolo_infusion.tiempo_medio': '20'}
        helpers.guardar(1, 2, dict(DATOS_COMUNES, **extra))
        self.assertEqual(informes.construir_contexto(1)[1]['cumple'], 'No')

    def test_bomba_sin_mediciones(self):
        helpers.guardar(1, 2, dict(DATOS_COMUNES))
        ctx = informes.construir_contexto(1)[1]
        self.assertEqual((ctx['cumple'], ctx['med']['flujo']), ('', ''))

    def test_concentrador(self):
        extra = {'verificación de llantas de transporte.': 'Cumple', 'Flujo 1': '1.4', 'Flujo 5': '7',
                 'Flujo 10': 'texto', 'concentrador.o2_medido': '92,5', 'Alarmas sonoras.': 'No cumple'}
        helpers.guardar(1, 4, dict(DATOS_COMUNES, **extra))
        plantilla, ctx = informes.construir_contexto(1)
        self.assertEqual(plantilla, 'pdf/concentrador_o2.html')
        llantas = next(i for i in ctx['inspeccion'] if 'llantas' in i['texto'])
        self.assertEqual(llantas['valor'], 'Cumple')       # antes se perdía por una clave mal escrita
        flujos = {f['programado']: f for f in ctx['flujos']}
        self.assertEqual((flujos[1]['error'], flujos[1]['estado']), ('+0.4', 'Cumple'))
        self.assertEqual(flujos[5]['estado'], 'No cumple')
        self.assertEqual(flujos[10]['estado'], '')          # texto no numérico: no se evalúa
        self.assertEqual(ctx['o2']['estado'], 'Cumple')
        html = informes.generar_reporte_html(1)
        self.assertIn('PM-013', html)
        self.assertNotIn('<<', html)

    def test_todos_los_tipos_generan_html(self):
        for tipo in informes.TIPOS_EQUIPO:
            with self.subTest(tipo=tipo):
                html = self.html(tipo, {})
                self.assertIn('PROTOCOLO DE MANTENIMIENTO', html)
                self.assertIn(informes.TIPOS_EQUIPO[tipo]['titulo'], html)

    def test_generico_lee_el_formulario(self):
        clave = 'Efecturar limpieza general del equipo.'
        html = self.html(5, {clave: 'cumple'})   # desfibrilador
        self.assertIn('Efecturar limpieza general del equipo.', html)
        _, ctx = informes.construir_contexto(1)
        total = sum(len(s['filas']) for s in ctx['secciones'])
        self.assertEqual(total, 42)                       # los 42 campos del formulario
        self.assertFalse(ctx['mostrar_limpieza_comun'])   # ya lo trae el formulario

    def test_generico_campos_repetidos_bascula(self):
        helpers.guardar(1, 1, dict(DATOS_COMUNES, verificacion=['si', 'no', 'na', 'nr'] + ['si'] * 5,
                                   volumen_medio='20', tiempo_medio='19'))
        _, ctx = informes.construir_contexto(1)
        valores = [f['valor'] for s in ctx['secciones'] for f in s['filas']][:4]
        self.assertEqual(valores, ['Cumple', 'No cumple', 'No aplica', 'No registra'])

    def test_formulario_vacio_no_rompe(self):
        html = self.html(16, {})     # ventilador: formulario aún vacío
        self.assertIn('todavía no tiene campos', html)

    def test_tipo_y_reporte_inexistentes(self):
        with self.assertRaises(informes.ReporteNoEncontrado):
            informes.construir_contexto(404)
        helpers.guardar(2, 99, dict(DATOS_COMUNES, **{'equipo.tipo': '99'}))
        with self.assertRaises(informes.TipoNoSoportado):
            informes.construir_contexto(2)
        helpers.REPORTES[3] = (3, 1, 7, 2, '2026-10-14', 'no es json')
        with self.assertRaises(informes.InformeInvalido):
            informes.construir_contexto(3)

    def test_html_escapa_contenido_malicioso(self):
        html = self.html(2, {'servicio.actividad': '<img src="file:///etc/passwd"><script>x()</script>'})
        self.assertNotIn('<img src="file:///etc/passwd">', html)
        self.assertNotIn('<script>x()', html)
        self.assertIn('&lt;img src=', html)

    def test_fecha_acepta_datetime(self):
        from datetime import date, datetime
        self.assertEqual(informes.fecha_legible(datetime(2026, 10, 14, 15, 30, 5, 123456)), '14/10/2026')
        self.assertEqual(informes.fecha_legible(date(2026, 10, 14)), '14/10/2026')
        self.assertEqual(informes.fecha_legible('2026-10-14 08:00:00'), '14/10/2026')

    def test_assets_dentro_de_static(self):
        self.assertTrue(informes.pdf_asset('images/pdf_logo.png').startswith('file://'))
        with self.assertRaises(ValueError):
            informes.pdf_asset('../app.py')


class Rutas(unittest.TestCase):
    def setUp(self):
        helpers.reiniciar()
        self.c = cliente_sesion()

    def test_descarga_pdf(self):
        original = informes.generar_reporte_pdf
        informes.generar_reporte_pdf = lambda i: b'%PDF-1.7 prueba'
        try:
            for url in ('/mantenimiento/reportes/5/descargar', '/mantenimiento/reportes/5/descargar/2'):
                r = self.c.get(url)
                self.assertEqual(r.status_code, 200, url)
                self.assertEqual(r.mimetype, 'application/pdf')
                self.assertIn('reporte_5.pdf', r.headers['Content-Disposition'])
                self.assertTrue(r.data.startswith(b'%PDF'))
        finally:
            informes.generar_reporte_pdf = original

    def test_descarga_reporte_inexistente(self):
        r = self.c.get('/mantenimiento/reportes/999/descargar')
        self.assertIn(r.status_code, (404, 500))   # 404 si hay WeasyPrint; 500 si falta en este entorno
        if r.status_code == 500:
            self.assertNotIn('Traceback', r.get_data(as_text=True))

    def test_descarga_falla_con_mensaje_generico(self):
        original = informes.generar_reporte_pdf
        informes.generar_reporte_pdf = lambda i: (_ for _ in ()).throw(OSError('cannot load library libpango'))
        try:
            r = self.c.get('/mantenimiento/reportes/1/descargar', headers={'X-Requested-With': 'XMLHttpRequest'})
        finally:
            informes.generar_reporte_pdf = original
        self.assertEqual(r.status_code, 500)
        self.assertNotIn('libpango', r.get_data(as_text=True))

    def test_historial_dashboard_clientes(self):
        helpers.guardar(1, 2, dict(DATOS_COMUNES))
        helpers.guardar(2, 4, dict(DATOS_COMUNES))
        html = self.c.get('/mantenimiento/historial').get_data(as_text=True)
        self.assertIn('Descargar PDF', html)
        self.assertIn('Clínica &lt;Norte&gt; &amp; Cía', html)
        self.assertIn('Bomba de infusión'.upper(), html.upper())
        self.assertEqual(self.c.get('/mantenimiento/historial?pagina=abc').status_code, 200)
        self.assertEqual(self.c.get('/dashboard').status_code, 200)
        self.assertIn('Otro', self.c.get('/clientes').get_data(as_text=True))

    def test_load_form_lista_blanca(self):
        self.assertEqual(self.c.get('/load_form/2').status_code, 200)
        self.assertEqual(self.c.get('/load_form/Protocolo_de_mantenimiento').status_code, 200)
        self.assertEqual(self.c.get('/load_form/../../app.py').status_code, 404)
        self.assertEqual(self.c.get('/load_form/99').status_code, 404)

    def test_todos_los_formularios_cargan(self):
        for i in range(1, 17):
            self.assertEqual(self.c.get(f'/load_form/{i}').status_code, 200, i)

    def test_healthz_publico(self):
        self.assertEqual(sisem.test_client().get('/healthz').get_json(), {'status': 'ok'})


def imagen_png(ancho=40, alto=30):
    from PIL import Image
    salida = io.BytesIO()
    Image.new('RGBA', (ancho, alto), (200, 30, 30, 128)).save(salida, 'PNG')
    return salida.getvalue()


HOJA_VALIDA = {
    'id_inventario': '4', 'modelo': 'X-200', 'registro_invima': 'INV-2020', 'area_servicio': 'UCI',
    'id_responsable': '1', 'fecha_adquisicion': '2024-03-15', 'periodicidad': 'Semestral',
    'control_metrologico': 'si', 'meses_mantenimiento': ['3', '9'], 'voltaje': '110',
    'manual_operacion': 'si', 'guia_rapida': 'no',
    'acc_cantidad_1': '2', 'acc_descripcion_1': 'Cánula <nasal>', 'acc_marca_1': '3',
    'acc_cantidad_3': '', 'acc_descripcion_3': '',          # filas vacías: no se guardan
}


class HojasDeVida(unittest.TestCase):
    def setUp(self):
        helpers.reiniciar()
        self.c = cliente_sesion()

    def post(self, datos, headers=CSRF):
        return self.c.post('/mantenimiento/hojas_vida_equipos/guardar', data=datos, headers=headers,
                           content_type='multipart/form-data')

    def test_paginas_exigen_login(self):
        anonimo = sisem.test_client()
        for url in ('/mantenimiento/hojas_vida_equipos', '/crear_hv', '/mantenimiento/hojas_vida_equipos/1/descargar'):
            self.assertEqual(anonimo.get(url).status_code, 302, url)

    def test_formulario_lista_equipos_y_datos_enlazados(self):
        html = self.c.get('/crear_hv').get_data(as_text=True)
        self.assertIn('data-inventario="MED_0077"', html)
        self.assertIn('data-entidad="Clínica &lt;Norte&gt; &amp; Cía"', html)   # escapado
        self.assertIn('data-telefono="3001234567"', html)
        self.assertIn('js/hoja_vida.js', html)
        self.assertNotRegex(html, r'<script(?![^>]*\bsrc=)')

    def test_guarda_con_foto_y_accesorios(self):
        datos = dict(HOJA_VALIDA, foto=(io.BytesIO(imagen_png()), '../../foto.png'))
        r = self.post(datos)
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(r.get_json()['archivo'], 'hoja_vida_MED_0077.pdf')
        guardada = helpers.HOJAS[r.get_json()['id']]
        d = guardada['datos']
        self.assertEqual((d['id_inventario'], d['id_responsable']), (4, 1))
        self.assertEqual(d['meses_mantenimiento'], [3, 9])
        self.assertIsNone(d['cubiculo'])                       # vacío -> NULL
        self.assertEqual(d['foto_mime'], 'image/jpeg')         # se recodifica como JPEG
        self.assertTrue(d['foto'].startswith(b'\xff\xd8'))
        self.assertEqual(d['foto_nombre'], 'foto.png')        # sin rutas
        self.assertEqual([a['fila'] for a in guardada['accesorios']], [1])
        self.assertEqual(guardada['accesorios'][0]['cantidad'], 2)
        # el equipo ya no aparece para otra hoja de vida y un segundo intento se rechaza
        self.assertNotIn('MED_0077', self.c.get('/crear_hv').get_data(as_text=True))
        self.assertEqual(self.post(HOJA_VALIDA).status_code, 400)

    def test_duplicado_por_carrera_devuelve_409(self):
        helpers.HOJAS[1] = {'datos': {'id_inventario': 4}, 'accesorios': []}
        original = helpers.fake.inventario_sin_hoja_vida
        helpers.fake.inventario_sin_hoja_vida = lambda: helpers.INVENTARIO   # otro usuario guardó a la vez
        try:
            self.assertEqual(self.post(HOJA_VALIDA).status_code, 409)
        finally:
            helpers.fake.inventario_sin_hoja_vida = original

    def test_validaciones(self):
        casos = {
            'sin equipo': dict(HOJA_VALIDA, id_inventario=''),
            'equipo inexistente': dict(HOJA_VALIDA, id_inventario='999'),
            'equipo inyección': dict(HOJA_VALIDA, id_inventario='4 OR 1=1'),
            'responsable inexistente': dict(HOJA_VALIDA, id_responsable='77'),
            'periodicidad libre': dict(HOJA_VALIDA, periodicidad='Cada rato'),
            'si/no inválido': dict(HOJA_VALIDA, ficha_tecnica='quizá'),
            'fecha inválida': dict(HOJA_VALIDA, fecha_adquisicion='2024-02-31'),
            'mes 13': dict(HOJA_VALIDA, meses_mantenimiento=['13']),
            'texto largo': dict(HOJA_VALIDA, cubiculo='x' * 61),
            'cantidad negativa': dict(HOJA_VALIDA, acc_cantidad_1='-1'),
            'marca accesorio': dict(HOJA_VALIDA, acc_marca_1='55'),
            'foto falsa': dict(HOJA_VALIDA, foto=(io.BytesIO(b'<script>x</script>'), 'foto.png')),
        }
        for nombre, datos in casos.items():
            with self.subTest(nombre):
                self.assertEqual(self.post(datos).status_code, 400)
        self.assertEqual(helpers.HOJAS, {})

    def test_sin_csrf(self):
        self.assertEqual(self.post(HOJA_VALIDA, headers={}).status_code, 400)
        self.assertEqual(helpers.HOJAS, {})

    def test_descarga_pdf(self):
        hoja_id = self.post(HOJA_VALIDA).get_json()['id']
        original = informes._html_a_pdf
        informes._html_a_pdf = lambda html: b'%PDF-1.7 prueba'
        try:
            r = self.c.get(f'/mantenimiento/hojas_vida_equipos/{hoja_id}/descargar')
        finally:
            informes._html_a_pdf = original
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, 'application/pdf')
        self.assertIn('hoja_vida_MED_0077.pdf', r.headers['Content-Disposition'])
        self.assertEqual(self.c.get('/mantenimiento/hojas_vida_equipos/999/descargar').status_code, 404)

    def test_html_del_pdf(self):
        hoja_id = self.post(dict(HOJA_VALIDA, foto=(io.BytesIO(imagen_png()), 'f.png'))).get_json()['id']
        with sisem.test_request_context():
            contexto = informes.contexto_hoja_vida(hoja_id)
            html = informes.render_template('pdf/hoja_de_vida.html', **contexto)
        for esperado in ('HOJA DE VIDA DE EQUIPO MÉDICO', 'PM_003', 'MED_0077', 'SN-77', 'Concentrador de O2',
                         'Clínica &lt;Norte&gt; &amp; Cía', 'Juan Pérez', '3001234567', '15/03/2024',
                         'Semestral', 'Cánula &lt;nasal&gt;', 'B. Braun', 'IIA', 'data:image/jpeg;base64,'):
            self.assertIn(esperado, html)
        self.assertEqual([m for m, marcado in contexto['meses'] if marcado], ['Mar.', 'Sep.'])
        self.assertEqual(len(contexto['accesorios']), 8)
        self.assertNotIn('<script', html)

    def test_consultar_lista_busca_y_enlaza_pdf(self):
        self.assertIn('Todavía no hay hojas de vida', self.c.get('/consultar_hv').get_data(as_text=True))
        self.post(HOJA_VALIDA)
        self.post(dict(HOJA_VALIDA, id_inventario='9'))

        todas = self.c.get('/consultar_hv').get_data(as_text=True)
        self.assertIn('2 resultados', todas)
        self.assertIn('Clínica &lt;Norte&gt; &amp; Cía', todas)                 # escapado
        self.assertIn('data-url="/mantenimiento/hojas_vida_equipos/1/descargar"', todas)
        self.assertIn('data-nombre="hoja_vida_MED_0077.pdf"', todas)
        self.assertIn('23/10/2026', todas)

        una = self.c.get('/consultar_hv?q=sn-99').get_data(as_text=True)
        self.assertIn('1 resultado', una)
        self.assertIn('MED/0099', una)
        self.assertNotIn('MED_0077', una)

        nada = self.c.get('/consultar_hv?q=%3Cscript%3E').get_data(as_text=True)
        self.assertIn('No se encontraron hojas de vida para «&lt;script&gt;»', nada)
        self.assertEqual(self.c.get('/consultar_hv?pagina=abc').status_code, 200)

    def test_nombre_de_archivo_seguro(self):
        self.assertEqual(informes.nombre_archivo_hoja_vida('MED/0099', 5), 'hoja_vida_MED_0099.pdf')
        self.assertEqual(informes.nombre_archivo_hoja_vida('../..', 5), 'hoja_vida_5.pdf')
        self.assertEqual(informes.nombre_archivo_hoja_vida(None, 5), 'hoja_vida_5.pdf')

    def test_pdf_solo_lee_recursos_de_static(self):
        permitido = informes.ruta_local_permitida
        self.assertTrue(permitido(informes.pdf_asset('images/pdf_logo.png')))
        self.assertTrue(permitido('data:image/png;base64,AAAA'))
        app_py = informes.Path(informes.APP_ROOT, 'app.py').as_uri()
        for url in (app_py, 'file:///etc/passwd', 'http://169.254.169.254/latest/meta-data/',
                    'https://ejemplo.com/x.png', 'ftp://ejemplo.com/x'):
            self.assertFalse(permitido(url), url)


if __name__ == '__main__':
    unittest.main()

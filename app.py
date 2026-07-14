from functools import wraps
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, send_file, after_this_request
from datetime import timedelta as tdelta
import json
import os
from datetime import datetime
import logging
from dotenv import load_dotenv
from database.db import personal, marcas, servicios, clientes, tipo_equipos, subir_reporte

sisem = Flask(__name__)

load_dotenv()

# SECURITY: Move to environment variables in production
sisem.secret_key = os.getenv('SECRET_KEY')

# AUTHENTICATION: TODO - Implement database authentication
user = os.getenv('DEFAULT_USER')
pws = os.getenv('DEFAULT_PASSWORD')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)



# Configuración de la sesión
sisem.permanent_session_lifetime = tdelta(minutes=15)
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"error":"not authenticated"}),401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# salida del sistema
@sisem.route('/logout')
@login_required
def logout():
    session.clear()
    return redirect(url_for('login'))

@sisem.route('/', methods=["GET", "POST"])
def home():
    return redirect(url_for('login'))

# ruta del login
@sisem.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if username == user and password == pws:
            session['username'] = username
            session.permanent = True
            return redirect(url_for('login_post'))
        else:
            return render_template('login.html', error='Credenciales inválidas')
    return render_template('login.html')


# Ingreso de estadísticas de la empresa
@sisem.route('/dashboard')
@login_required
def login_post():
        return render_template('dashboard.html')


# Acceso a base de datos de clientes
@sisem.route('/clientes')
@login_required
def ver_clientes():
    return render_template('clientes.html')


# Prueba de formulario
@sisem.route('/mantenimiento/reportes')
@login_required
def mantenimiento_correctivo():
    tecnicos = personal()
    brand = marcas()
    return render_template('forms/forms_mantenimiento.html', tecnicos=tecnicos, brand=brand)


# Ajax para el forms
@sisem.route('/load_form/<form_type>')
@login_required
def load_form(form_type):

    tecnicos_data = personal() or []
    brand_data = marcas() or []
    tipo_equipos_data = tipo_equipos() or []
    servicios_data = servicios() or []
    clientes_data = clientes() or []

    forms = {
        'reporte_general': 'forms/form_reporte_general.html',
        'Protocolo_de_mantenimiento': 'forms/form_protocolo_mantenimiento.html',
        'consulta_reporte': 'forms/form_consulta.html',
        '1': 'forms/protocolos_mantenimiento/bascula.html',
        '2': 'forms/protocolos_mantenimiento/bomba_infusion.html',
        '3': 'forms/protocolos_mantenimiento/cama_hospitalaria.html',
        '4': 'forms/protocolos_mantenimiento/concentrador_O2.html',
        '5': 'forms/protocolos_mantenimiento/desfibrilador.html',
        '6': 'forms/protocolos_mantenimiento/electrobisturi.html',
        '7': 'forms/protocolos_mantenimiento/electrocardiografo.html',
        '8': 'forms/protocolos_mantenimiento/flujometro.html',
        '9': 'forms/protocolos_mantenimiento/incubadora.html',
        '10': 'forms/protocolos_mantenimiento/maquina_anestesia.html',
        '11': 'forms/protocolos_mantenimiento/monitor_signos_vitales.html',
        '12': 'forms/protocolos_mantenimiento/neveras.html',
        '13': 'forms/protocolos_mantenimiento/oximetro.html',
        '14': 'forms/protocolos_mantenimiento/succionador.html',
        '15': 'forms/protocolos_mantenimiento/tensiometro.html',
        '16': 'forms/protocolos_mantenimiento/ventilador.html'
    }

    # ajax desde js
    template = forms.get(form_type)
    if template:
        return render_template(template, tecnicos=tecnicos_data, brand=brand_data, servicios=servicios_data, clientes=clientes_data, tipo_equipos=tipo_equipos_data)
    else:
        return jsonify({"error": "Tipo de formulario no encontrado"}), 404
    

# AJAX para guardar informe de mantenimiento
@sisem.route('/mantenimiento/reportes/guardar_informe', methods=['POST'])
@login_required
def guardar_informe():
    try:
        datos = request.get_json()
        
        if not datos:
            return jsonify({"error": "No data provided"}), 400
                
        # Validar que existan las claves requeridas
        claves_requeridas = ["tecnico.nombre", "cliente.nombre", "equipo.tipo", "general.fecha_mantenimiento"]
        for clave in claves_requeridas:
            if clave not in datos:
                return jsonify({"error": f"Falta la clave requerida: {clave}"}), 400
        
        tecnico = datos["tecnico.nombre"]
        cliente = datos["cliente.nombre"]
        equipo = datos["equipo.tipo"]
        fecha = datos["general.fecha_mantenimiento"]
        reporte = json.dumps(datos, ensure_ascii=False, indent=4)

        result = subir_reporte(tecnico, cliente, equipo, fecha, reporte)
        if not result:
            return jsonify({"error": "No se pudo guardar el informe"}), 500

        return jsonify({"mensaje": "Informe guardado correctamente", "id": result}), 201

    except Exception as e:
        logger.error(f"Error guardando informe: {str(e)}")
        print(f"Error guardando informe: {str(e)}")  # Debug
        return jsonify({"error": f"Error al guardar el informe: {str(e)}"}), 500

@sisem.route('/mantenimiento/reportes/<int:report_id>/descargar')
@login_required
def descargar_informe(report_id):
    try:
        # Import here to avoid potential circular imports at module load
        from informes.informes import generar_reporte_pdf_concentrador
        pdf_path = generar_reporte_pdf_concentrador(report_id)
        if not pdf_path or not os.path.exists(pdf_path):
            return jsonify({"error": "No se pudo generar el PDF"}), 500

        @after_this_request
        def _remove_file(response):
            try:
                os.remove(pdf_path)
            except Exception:
                pass
            return response

        return send_file(pdf_path, as_attachment=True, download_name=f"reporte_{report_id}.pdf", mimetype='application/pdf')
    except Exception as e:
        logger.error(f"Error generando/descargando PDF: {e}")
        return jsonify({"error": f"Error generando/descargando PDF: {str(e)}"}), 500


if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'True') == 'True'
    sisem.run(debug=debug_mode, port=5000)
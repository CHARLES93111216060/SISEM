from functools import wraps
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from datetime import timedelta as tdelta
import json
import os
from datetime import datetime
import logging
from dotenv import load_dotenv

sisem = Flask(__name__)

load_dotenv()

# SECURITY: Move to environment variables in production
sisem.secret_key = os.getenv('SECRET_KEY')

# AUTHENTICATION: TODO - Implement database authentication
user = os.getenv('DEFAULT_USER')
print(user)
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

# ruta del login
@sisem.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if username == user and password == pws:
            session['username'] = username
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
def clientes():
    return render_template('clientes.html')


# Prueba de formulario
@sisem.route('/mantenimiento/reportes')
@login_required
def mantenimiento_correctivo():
    return render_template('/forms/forms_mantenimiento.html')


# Ajax para el forms
@sisem.route('/load_form/<form_type>')
@login_required
def load_form(form_type):

    forms = {
        'reporte_general': 'forms/form_reporte_general.html',
        'Protocolo_de_mantenimiento': 'forms/form_protocolo_mantenimiento.html',
        'consulta_reporte': 'forms/form_consulta.html',
        'bascula': 'forms/protocolos_mantenimiento/bascula.html',
        'Bomba_de_infusión': 'forms/protocolos_mantenimiento/bomba_infusion.html',
        'Cama_hospitalaria': 'forms/protocolos_mantenimiento/cama_hospitalaria.html',
        'Concentrador_de_O2': 'forms/protocolos_mantenimiento/concentrador_O2.html',
        'Desfibrilador': 'forms/protocolos_mantenimiento/desfibrilador.html',
        'Electrobisturí': 'forms/protocolos_mantenimiento/electrobisturi.html',
        'Electrocardiógrafo': 'forms/protocolos_mantenimiento/electrocardiografo.html',
        'Flujómetro': 'forms/protocolos_mantenimiento/flujometro.html',
        'Incubadora': 'forms/protocolos_mantenimiento/incubadora.html',
        'Máquina_de_anestésia': 'forms/protocolos_mantenimiento/maquina_anestesia.html',
        'Monitor_de_signos_vitales': 'forms/protocolos_mantenimiento/monitor_signos_vitales.html',
        'Neveras': 'forms/protocolos_mantenimiento/neveras.html',
        'Oxímetro': 'forms/protocolos_mantenimiento/oximetro.html',
        'Succionador': 'forms/protocolos_mantenimiento/succionador.html',
        'Tensiómetro': 'forms/protocolos_mantenimiento/tensiometro.html',
        'ventilador': 'forms/protocolos_mantenimiento/ventilador.html'
    }

    # ajax desde js
    template = forms.get(form_type)
    if template:
        return render_template(template)
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
        
        os.makedirs('informes', exist_ok=True)
        fecha = datetime.now().strftime("%Y%m%d_%H%M%S")
        archivo = f'informes/informe_{fecha}.json'

        with open(archivo, 'w', encoding='utf-8') as f:
            json.dump(datos, f, ensure_ascii=False, indent=4)
        
        logger.info(f"Informe guardado: {archivo}")
        return jsonify({"mensaje": "Informe guardado correctamente"}), 201

    except Exception as e:
        logger.error(f"Error guardando informe: {str(e)}")
        return jsonify({"error": "Error al guardar el informe"}), 500
    

if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'True') == 'True'
    sisem.run(debug=debug_mode, port=5000)
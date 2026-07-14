import os
from dotenv import load_dotenv
import json
import mysql.connector as connection

load_dotenv()
# -----------------------------------------------------------------------------------------
def conexiones():
    try:
        conexion = connection.connect(
            host=os.getenv('db_host'),
            user=os.getenv('db_user'),
            password=os.getenv('db_password'),
            database=os.getenv('db_name'),
            port=os.getenv('db_port')
        )
        if conexion:
            # print("...Conexión exitosa a la base de datos...")
            return conexion
        else:
            print("...falla en la conexión...")
    except Exception as e:
        print(f"error: Algo salió mal {e}")
# -----------------------------------------------------------------------------------------
def servicios():
    consulta = "SELECT * FROM mantenimientos;"
    try:
        conexion = conexiones()
        if conexion:
            cursor = conexion.cursor()
            cursor.execute(consulta)
            datos = cursor.fetchall()
            cursor.close()
            conexion.close()
            return datos
        else:
            print("...falla en la conexión...")
    except Exception as e:
        print(f"error: Algo salió mal {e}")
# -----------------------------------------------------------------------------------------
def personal():
    consulta = "SELECT * FROM personal;"
    try:
        conexion = conexiones()
        if conexion:
            cursor = conexion.cursor()
            cursor.execute(consulta)
            datos = cursor.fetchall()
            cursor.close()
            conexion.close()
            return datos
        else:
            print("...falla en la conexión...")
    except Exception as e:
        print(f"error: Algo salió mal {e}")
# -----------------------------------------------------------------------------------------
def marcas():
    consulta = "SELECT * FROM marcas;"
    try:
        conexion = conexiones()
        if conexion:
            cursor = conexion.cursor()
            cursor.execute(consulta)
            datos = cursor.fetchall()
            cursor.close()
            conexion.close()
            return datos
    except Exception as e:
        print(f"Error: se ha presentado un error con la conexion: {e}")
# -----------------------------------------------------------------------------------------
def clientes():
    consulta = "SELECT * FROM clientes;"
    try:
        conexion = conexiones()
        if conexion:
            cursor = conexion.cursor()
            cursor.execute(consulta)
            datos = cursor.fetchall()
            cursor.close()
            conexion.close()
            return datos
    except Exception as e:
        print(f"Error: se ha presentado un error con la conexion: {e}")
# -----------------------------------------------------------------------------------------
def tipo_equipos():
    consulta = "SELECT * FROM tipo_equipo;"
    try:
        conexion = conexiones()
        if conexion:
            cursor = conexion.cursor()
            cursor.execute(consulta)
            datos = cursor.fetchall()
            cursor.close()
            conexion.close()
            return datos
    except Exception as e:
        print(f"Error: se ha presentado un error con la conexion: {e}")
# -----------------------------------------------------------------------------------------
def subir_reporte(tecnico, cliente, equipo, fecha, reporte):
    consulta = "INSERT INTO reportes (id_tecnico, id_cliente, id_equipo, fecha, reporte) VALUES (%s, %s, %s, %s, %s);"
    try:
        conexion = conexiones()
        if conexion:
            cursor = conexion.cursor()
            cursor.execute(consulta, (tecnico, cliente, equipo, fecha, reporte))
            conexion.commit()
            last_id = cursor.lastrowid
            print(f"...Reporte guardado correctamente... con el id {last_id}")
            cursor.close()
            conexion.close()
            return last_id

    except Exception as e:
        print(f"Error: se ha presentado un error con la conexion: {e}")

    return False
# -----------------------------------------------------------------------------------------
def obtener_reporte_por_id(reporte_id):
    consulta = "SELECT * FROM reportes WHERE id = %s;"
    try:
        conexion = conexiones()
        if conexion:
            cursor = conexion.cursor()
            cursor.execute(consulta, (reporte_id,))
            datos = cursor.fetchone()
            cursor.close()
            conexion.close()
            return datos
    except Exception as e:
        print(f"Error: se ha presentado un error con la conexion: {e}")


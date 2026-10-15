"""Pruebas de database/db.py con un pool simulado (no hace falta PostgreSQL ni psycopg)."""
import importlib.util
import os
import sys
import types
import unittest
from unittest import mock

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


class FalsoCursor:
    def __init__(self, registro, filas):
        self.registro, self.filas = registro, list(filas)

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=()): self.registro.append((' '.join(sql.split()), tuple(params)))
    def fetchone(self): return self.filas.pop(0) if self.filas else None
    def fetchall(self): return self.filas


class FalsaConexion:
    def __init__(self, registro, filas):
        self.registro, self.filas = registro, filas

    def __enter__(self): return self
    def __exit__(self, tipo, *a): self.registro.append(('FIN', 'rollback' if tipo else 'commit')); return False
    def cursor(self): return FalsoCursor(self.registro, self.filas)


class FalsoPool:
    creados = []
    registro = []
    filas = []
    falla = False

    def __init__(self, conninfo='', **kw):
        self.conninfo, self.kw = conninfo, kw
        FalsoPool.creados.append(self)

    def connection(self):
        if FalsoPool.falla:
            raise RuntimeError('base caída')
        return FalsaConexion(FalsoPool.registro, FalsoPool.filas)


def cargar_db():
    falso = types.ModuleType('psycopg_pool')
    falso.ConnectionPool = FalsoPool
    with mock.patch.dict(sys.modules, {'psycopg_pool': falso}):
        spec = importlib.util.spec_from_file_location('db_postgres_prueba', os.path.join(RAIZ, 'database', 'db.py'))
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
    return modulo


class BaseDatos(unittest.TestCase):
    def setUp(self):
        FalsoPool.creados.clear(); FalsoPool.registro.clear(); FalsoPool.filas = []; FalsoPool.falla = False
        self.db = cargar_db()

    def entorno(self, **variables):
        limpio = {k: v for k, v in os.environ.items()
                  if k.lower() not in ('database_url', 'db_host', 'db_port', 'db_user', 'db_password', 'db_name')}
        limpio.update(variables)
        return mock.patch.dict(os.environ, limpio, clear=True)

    def test_usa_database_url(self):
        with self.entorno(DATABASE_URL='postgresql://u:p@host/base?sslmode=require'):
            self.db.marcas()
        pool = FalsoPool.creados[0]
        self.assertEqual(pool.conninfo, 'postgresql://u:p@host/base?sslmode=require')
        self.assertNotIn('host', pool.kw['kwargs'])

    def test_variables_sueltas_si_no_hay_url(self):
        with self.entorno(db_host='h', db_port='5433', db_user='u', db_password='p', db_name='n'):
            self.db.marcas()
        kw = FalsoPool.creados[0].kw['kwargs']
        self.assertEqual((kw['host'], kw['port'], kw['user'], kw['password'], kw['dbname']), ('h', 5433, 'u', 'p', 'n'))

    def test_subir_reporte_usa_returning(self):
        FalsoPool.filas = [(58,)]
        with self.entorno(DATABASE_URL='x'):
            nuevo = self.db.subir_reporte(1, 7, 2, '2026-10-14', '{"a": "b"}')
        self.assertEqual(nuevo, (58, 2))
        sql, params = FalsoPool.registro[0]
        self.assertIn('RETURNING id', sql)
        self.assertEqual(params, (1, 7, 2, '2026-10-14', '{"a": "b"}'))
        self.assertEqual(FalsoPool.registro[-1], ('FIN', 'commit'))

    def test_consultas_de_catalogos_son_explicitas(self):
        with self.entorno(DATABASE_URL='x'):
            for funcion in (self.db.servicios, self.db.personal, self.db.marcas, self.db.clientes, self.db.tipo_equipos):
                funcion()
        consultas = [c[0] for c in FalsoPool.registro if c[0] != 'FIN']
        self.assertEqual(len(consultas), 5)
        self.assertTrue(all('SELECT id, ' in c and '*' not in c for c in consultas))

    def test_contar_con_fecha_y_listar(self):
        FalsoPool.filas = [(3,)]
        with self.entorno(DATABASE_URL='x'):
            self.assertEqual(self.db.contar_reportes('2026-10-01'), 3)
            self.db.listar_reportes(20, 40)
        self.assertIn('::date', FalsoPool.registro[0][0])
        self.assertEqual(FalsoPool.registro[2][1], (20, 40))

    def test_lecturas_toleran_fallos_y_escrituras_no(self):
        FalsoPool.falla = True
        with self.entorno(DATABASE_URL='x'):
            self.assertEqual(self.db.marcas(), [])
            self.assertIsNone(self.db.contar_reportes())
            with self.assertRaises(RuntimeError):
                self.db.subir_reporte(1, 1, 1, '2026-10-14', '{}')


if __name__ == '__main__':
    unittest.main()

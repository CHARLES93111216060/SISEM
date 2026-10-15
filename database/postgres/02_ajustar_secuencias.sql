-- =====================================================================================
-- Ejecutar DESPUÉS de cargar los datos.
-- Al copiar filas con su id original, el contador de autonumeración sigue en 1 y el
-- siguiente INSERT chocaría con un id existente. Este script lo pone después del mayor id.
-- =====================================================================================
SELECT setval(pg_get_serial_sequence('marcas',         'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM marcas;
SELECT setval(pg_get_serial_sequence('tipo_equipo',    'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM tipo_equipo;
SELECT setval(pg_get_serial_sequence('mantenimientos', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM mantenimientos;
SELECT setval(pg_get_serial_sequence('clientes',       'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM clientes;
SELECT setval(pg_get_serial_sequence('personal',       'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM personal;
SELECT setval(pg_get_serial_sequence('inventario',     'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM inventario;
SELECT setval(pg_get_serial_sequence('reportes',       'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM reportes;

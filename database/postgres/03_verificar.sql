-- =====================================================================================
-- Conteo de filas por tabla. Ejecuta la MISMA consulta en MariaDB y en PostgreSQL:
-- los números deben coincidir.   (Sintaxis válida en ambos motores.)
-- =====================================================================================
SELECT 'marcas'         AS tabla, COUNT(*) AS filas FROM marcas
UNION ALL SELECT 'tipo_equipo',    COUNT(*) FROM tipo_equipo
UNION ALL SELECT 'mantenimientos', COUNT(*) FROM mantenimientos
UNION ALL SELECT 'clientes',       COUNT(*) FROM clientes
UNION ALL SELECT 'personal',       COUNT(*) FROM personal
UNION ALL SELECT 'inventario',     COUNT(*) FROM inventario
UNION ALL SELECT 'reportes',       COUNT(*) FROM reportes;

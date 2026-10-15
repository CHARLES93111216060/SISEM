-- =====================================================================================
-- Ejecutar en MARIADB (conexión actual de DBeaver) para confirmar los tipos reales
-- antes de crear el esquema en PostgreSQL. Compara con 01_esquema.sql.
-- =====================================================================================
SELECT table_name, ordinal_position AS pos, column_name, column_type, is_nullable
FROM information_schema.columns
WHERE table_schema = DATABASE()
ORDER BY table_name, ordinal_position;

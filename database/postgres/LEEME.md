# Migrar SISEM de MariaDB a PostgreSQL (Render)

Archivos de esta carpeta, en el orden en que se usan:

| Archivo | Dónde se ejecuta | Para qué |
|---|---|---|
| `00_tipos_en_mariadb.sql` | MariaDB | Confirmar los tipos reales de columnas (el diagrama no los muestra) |
| `01_esquema.sql` | PostgreSQL (Render) | Crear las 7 tablas, claves foráneas e índices |
| *(copia de datos con DBeaver)* | — | Ver paso 4 |
| `02_ajustar_secuencias.sql` | PostgreSQL (Render) | Que los nuevos ids continúen después del mayor existente |
| `03_verificar.sql` | Ambas | Comparar el número de filas por tabla |

## Antes de empezar

- El PostgreSQL **gratis** de Render expira a los 30 días de creado (después hay 14 días de gracia y se borra),
  tiene 1 GB, no hace backups y solo se permite uno por cuenta. Úsalo para pruebas.
  Para datos reales, pasa a un plan de pago **antes** de que expire. Mientras tanto, **conserva tu MariaDB local como fuente principal.**
- Haz una copia de seguridad de MariaDB antes de tocar nada:
  `mysqldump -u USUARIO -p NOMBRE_BD > sisem_respaldo.sql` (o DBeaver → clic derecho en la base → *Tools → Dump database*).

## Paso a paso

### 1. Crear la base en Render
Dashboard → **New → PostgreSQL** → plan *Free* → región **la misma que tendrá tu servicio web**.
Cuando esté lista, en la pestaña *Info* copia: Hostname, Port, Database, Username, Password,
**External Database URL** (para DBeaver y tu computador) e **Internal Database URL** (para la app en Render).

### 2. Conectar DBeaver a Render
*Nueva conexión → PostgreSQL* con host, puerto 5432, base, usuario y contraseña de Render.
En la pestaña **SSL** activa *Use SSL* (modo `require`); sin SSL Render rechaza la conexión externa.
Si no conecta, revisa *Access Control* en el panel de la base.

### 3. Crear el esquema
1. En MariaDB ejecuta `00_tipos_en_mariadb.sql` y compara con `01_esquema.sql`.
   Si `reportes.fecha` es `DATETIME`, cambia `date` por `timestamp` en el script.
2. Abre `01_esquema.sql` sobre la conexión de Render y ejecútalo como **script** (Alt+X).

### 4. Copiar los datos
En la conexión de MariaDB: selecciona las tablas → clic derecho → **Export Data → Database table(s)**.

- *Target container*: la conexión de Render, esquema `public`.
- En *Tables mapping* cada tabla debe decir **existing** (ya las creó el script), no *create*.
- En *Data load settings* marca **Transfer auto-generated columns** (si no, DBeaver no copia los `id`).
- No marques *Disable referential integrity checks* (Render no da permisos de superusuario).
- Hazlo en **dos pasadas** para respetar las claves foráneas:
  1. `marcas`, `tipo_equipo`, `mantenimientos`, `clientes`, `personal`
  2. `inventario`, `reportes`

### 5. Ajustar secuencias y verificar
Ejecuta `02_ajustar_secuencias.sql` en Render. Luego ejecuta `03_verificar.sql` en MariaDB y en Render:
los conteos deben ser iguales.

### 6. Apuntar la app a PostgreSQL
- En tu computador (`.env`): `DATABASE_URL=<External Database URL>?sslmode=require`
- En Render (servicio web → *Environment*): `DATABASE_URL=<Internal Database URL>`
- Instala el driver nuevo: `pip install -r requirements.txt` (ya trae `psycopg`).

Prueba: inicia sesión, abre *Historial* y descarga el PDF de un reporte existente.

## Alternativas para copiar los datos

- **pgloader** (Linux/Docker): migra esquema y datos en un comando,
  `pgloader mysql://usuario:clave@host/sisem "postgresql://usuario:clave@host/base?sslmode=require"`.
  Crea las tablas con su propia conversión de tipos, así que no uses `01_esquema.sql` con este método.
- **CSV**: exporta cada tabla a CSV desde DBeaver e impórtala con `\copy tabla FROM 'archivo.csv' CSV HEADER`.

## Respaldo periódico de Render

```bash
pg_dump "EXTERNAL_DATABASE_URL" -Fc -f sisem_$(date +%F).dump
```

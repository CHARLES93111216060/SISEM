-- =====================================================================================
-- SISEM · Roles del personal y módulos de Comercial, Talento humano y Laboratorio
--
-- Solo AGREGA columnas y tablas (no borra ni modifica datos existentes, salvo poner
-- 'funcionario' en personal.funcion cuando está vacía). Se puede ejecutar varias veces:
-- todo usa IF NOT EXISTS. Ejecútalo en la base local y en la de la nube (Render).
--
--   psql "$DATABASE_URL" -f database/postgres/04_roles_y_modulos.sql
--   (o ábrelo en DBeaver y ejecútalo completo con Alt+X)
-- =====================================================================================
BEGIN;

-- -------------------------------------------------------------------------------------
-- 1. Acceso del personal (rol "funcionario")
--    El administrador sigue definido en las variables de entorno (ADMIN_USER / ADMIN_PASSWORD_HASH).
-- -------------------------------------------------------------------------------------
ALTER TABLE personal ADD COLUMN IF NOT EXISTS funcion       varchar(30);
ALTER TABLE personal ADD COLUMN IF NOT EXISTS usuario       varchar(60);
ALTER TABLE personal ADD COLUMN IF NOT EXISTS clave_hash    text;
ALTER TABLE personal ADD COLUMN IF NOT EXISTS acceso_activo boolean   NOT NULL DEFAULT false;
ALTER TABLE personal ADD COLUMN IF NOT EXISTS ultimo_acceso timestamp;

UPDATE personal SET funcion = 'funcionario' WHERE funcion IS NULL OR btrim(funcion) = '';
ALTER TABLE personal ALTER COLUMN funcion SET DEFAULT 'funcionario';

-- El usuario no distingue mayúsculas y no se puede repetir (los vacíos sí).
CREATE UNIQUE INDEX IF NOT EXISTS personal_usuario_key ON personal (lower(usuario)) WHERE usuario IS NOT NULL;

-- -------------------------------------------------------------------------------------
-- 2. Comercial · Facturación (control interno de cobros; NO es factura electrónica DIAN)
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS facturas (
    id                serial PRIMARY KEY,
    id_cliente        integer       NOT NULL REFERENCES clientes(id),
    fecha_emision     date          NOT NULL,
    fecha_vencimiento date          NOT NULL,
    estado            varchar(20)   NOT NULL DEFAULT 'pendiente'
                      CHECK (estado IN ('pendiente', 'pagada', 'anulada')),
    observaciones     text,
    subtotal          numeric(14,2) NOT NULL DEFAULT 0,
    iva               numeric(14,2) NOT NULL DEFAULT 0,
    total             numeric(14,2) NOT NULL DEFAULT 0,
    creado_por        varchar(120),
    creado_en         timestamp     NOT NULL DEFAULT now(),
    CONSTRAINT facturas_fechas CHECK (fecha_vencimiento >= fecha_emision)
);

CREATE TABLE IF NOT EXISTS facturas_items (
    id             serial PRIMARY KEY,
    id_factura     integer       NOT NULL REFERENCES facturas(id) ON DELETE CASCADE,
    fila           smallint      NOT NULL,
    descripcion    varchar(255)  NOT NULL,
    cantidad       numeric(12,2) NOT NULL CHECK (cantidad > 0),
    valor_unitario numeric(14,2) NOT NULL CHECK (valor_unitario >= 0),
    iva_pct        numeric(5,2)  NOT NULL DEFAULT 0 CHECK (iva_pct BETWEEN 0 AND 100),
    UNIQUE (id_factura, fila)
);

-- -------------------------------------------------------------------------------------
-- 3. Comercial · Órdenes de compra (a proveedores)
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ordenes_compra (
    id             serial PRIMARY KEY,
    proveedor      varchar(150)  NOT NULL,
    nit_proveedor  varchar(30),
    contacto       varchar(150),
    fecha          date          NOT NULL,
    fecha_entrega  date,
    estado         varchar(20)   NOT NULL DEFAULT 'pendiente'
                   CHECK (estado IN ('pendiente', 'aprobada', 'recibida', 'anulada')),
    observaciones  text,
    subtotal       numeric(14,2) NOT NULL DEFAULT 0,
    iva            numeric(14,2) NOT NULL DEFAULT 0,
    total          numeric(14,2) NOT NULL DEFAULT 0,
    creado_por     varchar(120),
    creado_en      timestamp     NOT NULL DEFAULT now(),
    CONSTRAINT ordenes_fechas CHECK (fecha_entrega IS NULL OR fecha_entrega >= fecha)
);

CREATE TABLE IF NOT EXISTS ordenes_compra_items (
    id             serial PRIMARY KEY,
    id_orden       integer       NOT NULL REFERENCES ordenes_compra(id) ON DELETE CASCADE,
    fila           smallint      NOT NULL,
    descripcion    varchar(255)  NOT NULL,
    cantidad       numeric(12,2) NOT NULL CHECK (cantidad > 0),
    valor_unitario numeric(14,2) NOT NULL CHECK (valor_unitario >= 0),
    iva_pct        numeric(5,2)  NOT NULL DEFAULT 0 CHECK (iva_pct BETWEEN 0 AND 100),
    UNIQUE (id_orden, fila)
);

-- -------------------------------------------------------------------------------------
-- 4. Talento humano · Cuentas de cobro (las radica el funcionario, las gestiona el admin)
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cuentas_cobro (
    id             serial PRIMARY KEY,
    id_personal    integer       NOT NULL REFERENCES personal(id),
    fecha          date          NOT NULL,
    periodo_inicio date          NOT NULL,
    periodo_fin    date          NOT NULL,
    concepto       varchar(500)  NOT NULL,
    valor          numeric(14,2) NOT NULL CHECK (valor > 0),
    estado         varchar(20)   NOT NULL DEFAULT 'pendiente'
                   CHECK (estado IN ('pendiente', 'aprobada', 'pagada', 'rechazada')),
    observaciones  text,
    creado_por     varchar(120),
    creado_en      timestamp     NOT NULL DEFAULT now(),
    CONSTRAINT cuentas_cobro_periodo CHECK (periodo_fin >= periodo_inicio)
);
CREATE INDEX IF NOT EXISTS cuentas_cobro_personal_idx ON cuentas_cobro (id_personal);

-- -------------------------------------------------------------------------------------
-- 5. Talento humano · Nómina (una liquidación por empleado y mes)
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS nomina (
    id                 serial PRIMARY KEY,
    id_personal        integer       NOT NULL REFERENCES personal(id),
    periodo            date          NOT NULL,              -- primer día del mes liquidado
    salario_base       numeric(14,2) NOT NULL CHECK (salario_base >= 0),
    dias_trabajados    smallint      NOT NULL CHECK (dias_trabajados BETWEEN 0 AND 30),
    auxilio_transporte numeric(14,2) NOT NULL DEFAULT 0 CHECK (auxilio_transporte >= 0),
    horas_extra        numeric(14,2) NOT NULL DEFAULT 0 CHECK (horas_extra >= 0),
    bonificaciones     numeric(14,2) NOT NULL DEFAULT 0 CHECK (bonificaciones >= 0),
    salud              numeric(14,2) NOT NULL DEFAULT 0,
    pension            numeric(14,2) NOT NULL DEFAULT 0,
    otras_deducciones  numeric(14,2) NOT NULL DEFAULT 0 CHECK (otras_deducciones >= 0),
    total_devengado    numeric(14,2) NOT NULL DEFAULT 0,
    total_deducciones  numeric(14,2) NOT NULL DEFAULT 0,
    neto_pagar         numeric(14,2) NOT NULL DEFAULT 0,
    observaciones      text,
    creado_por         varchar(120),
    creado_en          timestamp     NOT NULL DEFAULT now(),
    CONSTRAINT nomina_unica UNIQUE (id_personal, periodo),
    CONSTRAINT nomina_periodo CHECK (extract(day FROM periodo) = 1)
);

-- -------------------------------------------------------------------------------------
-- 6. Laboratorio · Metrología (cronograma y reportes de calibración / verificación)
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metrologia_cronograma (
    id               serial PRIMARY KEY,
    id_equipo        integer      NOT NULL REFERENCES inventario(id),
    tipo             varchar(20)  NOT NULL CHECK (tipo IN ('calibracion', 'verificacion', 'calificacion')),
    fecha_programada date         NOT NULL,
    estado           varchar(20)  NOT NULL DEFAULT 'pendiente'
                     CHECK (estado IN ('pendiente', 'en_proceso', 'realizado', 'cancelado')),
    proveedor        varchar(150),
    id_responsable   integer      REFERENCES personal(id) ON DELETE SET NULL,
    observaciones    text,
    creado_en        timestamp    NOT NULL DEFAULT now(),
    CONSTRAINT metrologia_cronograma_unico UNIQUE (id_equipo, tipo, fecha_programada)
);
CREATE INDEX IF NOT EXISTS metrologia_cronograma_fecha_idx ON metrologia_cronograma (fecha_programada);

CREATE TABLE IF NOT EXISTS metrologia_reportes (
    id                 serial PRIMARY KEY,
    id_equipo          integer      NOT NULL REFERENCES inventario(id),
    id_cronograma      integer      REFERENCES metrologia_cronograma(id) ON DELETE SET NULL,
    tipo               varchar(20)  NOT NULL CHECK (tipo IN ('calibracion', 'verificacion', 'calificacion')),
    fecha              date         NOT NULL,
    id_tecnico         integer      NOT NULL REFERENCES personal(id),
    patron             varchar(150) NOT NULL,
    certificado_patron varchar(80),
    magnitud           varchar(60)  NOT NULL,
    unidad             varchar(20)  NOT NULL,
    mediciones         jsonb        NOT NULL,
    resultado          varchar(15)  NOT NULL CHECK (resultado IN ('conforme', 'no_conforme')),
    proxima_fecha      date,
    observaciones      text,
    creado_por         varchar(120),
    creado_en          timestamp    NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS metrologia_reportes_equipo_idx ON metrologia_reportes (id_equipo);

COMMIT;

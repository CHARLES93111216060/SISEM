# SISEM — Sistema Integrado de Seguimiento de Equipos Médicos

Aplicación Flask para registrar mantenimientos de equipos biomédicos y descargar el informe en PDF.

## Estructura del proyecto

```text
sisem/
├── app.py                          # Aplicación Flask: rutas, login, CSRF, validación, cabeceras
├── requirements.txt                # Dependencias de Python
├── Dockerfile                      # Imagen para Render (incluye Pango para WeasyPrint)
├── render.yaml                     # Definición del servicio en Render
├── .env.example                    # Plantilla de variables de entorno (el .env real no se sube)
├── .dockerignore
├── .gitignore
├── README.md
│
├── database/
│   ├── __init__.py
│   └── db.py                       # Pool MySQL, catálogos y consultas de la tabla reportes
│
├── informes/
│   ├── __init__.py
│   └── informes.py                 # Construye el contexto del informe y genera el PDF (WeasyPrint)
│
├── templates/
│   ├── login.html
│   ├── dashboard.html
│   ├── clientes.html
│   ├── historial.html              # Listado paginado de reportes con botón de descarga
│   ├── proximamente.html           # Módulos del menú aún no implementados
│   ├── error.html
│   ├── layout/
│   │   ├── base.html
│   │   ├── header.html
│   │   ├── sidebar.html
│   │   └── footer.html
│   ├── forms/
│   │   ├── forms_mantenimiento.html          # Página con el selector de formularios
│   │   ├── form_protocolo_mantenimiento.html # Formulario común (técnico, cliente, equipo, servicio…)
│   │   ├── form_reporte_general.html
│   │   ├── form_consulta.html
│   │   └── protocolos_mantenimiento/         # Un formulario por tipo de equipo (ids 1-16)
│   │       ├── bascula.html
│   │       ├── bomba_infusion.html
│   │       ├── cama_hospitalaria.html
│   │       ├── concentrador_O2.html
│   │       ├── desfibrilador.html
│   │       ├── electrobisturi.html
│   │       ├── electrocardiografo.html
│   │       ├── flujometro.html
│   │       ├── incubadora.html
│   │       ├── maquina_anestesia.html
│   │       ├── monitor_signos_vitales.html
│   │       ├── neveras.html
│   │       ├── oximetro.html
│   │       ├── succionador.html
│   │       ├── tensiometro.html
│   │       └── ventilador.html
│   └── pdf/                        # Plantillas HTML + CSS que se convierten a PDF
│       ├── base.html
│       ├── _estilos.css
│       ├── _encabezado.html
│       ├── _cliente.html
│       ├── _equipo.html
│       ├── _servicio.html
│       ├── _repuestos.html
│       ├── _cierre.html
│       ├── _pie.html
│       ├── bomba_infusion.html     # Réplica del formato Word PM-020
│       ├── concentrador_o2.html    # Réplica del formato Word PM-013
│       └── protocolo_generico.html # Resto de equipos
│
├── static/
│   ├── css/
│   │   ├── style.css
│   │   └── login.css
│   ├── js/
│   │   ├── app.js                  # Token CSRF y descarga de PDF por fetch
│   │   ├── forms.js                # Carga dinámica de formularios y guardado del informe
│   │   └── historial.js            # Descarga de PDF desde el historial
│   ├── fonts/                      # Jost (Regular, Medium, Bold), usada en el PDF
│   └── images/                     # Íconos SVG, logos y sello del PDF (pdf_logo.png, pdf_sello.png)
│
├── Formatos/                       # Formatos Word originales (referencia de diseño, no se usan en ejecución)
│
├── tests/
│   ├── __init__.py
│   ├── helpers.py                  # Base de datos simulada
│   └── test_sisem.py
│
└── tools/
    └── generar_hash.py             # Genera ADMIN_PASSWORD_HASH
```

`sisemVM/` (entorno virtual), `__pycache__/` y `.env` son locales y no forman parte del código.

## Flujo de datos y endpoints

### Endpoints

| Método | Ruta                                                | Sesión    | Qué hace                                                                                                                                                                                            | Responde                                              |
| ------- | --------------------------------------------------- | ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| GET     | `/healthz`                                        | No         | Chequeo de salud para Render                                                                                                                                                                         | JSON`{"status": "ok"}`                              |
| GET     | `/`                                               | No         | Redirige al login                                                                                                                                                                                    | 302 →`/login`                                      |
| GET     | `/login`                                          | No         | Formulario de acceso (si ya hay sesión, va al dashboard)                                                                                                                                            | HTML                                                  |
| POST    | `/login`                                          | No         | Verifica usuario y hash de la clave; bloquea tras 5 fallos por IP                                                                                                                                    | 302 →`/dashboard` · 401 · 429                    |
| POST    | `/logout`                                         | No         | Cierra la sesión                                                                                                                                                                                    | 302 →`/login`                                      |
| GET     | `/dashboard`                                      | Sí        | Totales de reportes (global y del mes) y número de clientes                                                                                                                                         | HTML                                                  |
| GET     | `/clientes`                                       | Sí        | Listado de clientes                                                                                                                                                                                  | HTML                                                  |
| GET     | `/mantenimiento/reportes`                         | Sí        | Página con el selector de formularios                                                                                                                                                               | HTML                                                  |
| GET     | `/load_form/<form_type>`                          | Sí        | Devuelve un fragmento de formulario con los catálogos cargados.`form_type` = `Protocolo_de_mantenimiento`, `reporte_general`, `consulta_reporte` o el id del tipo de equipo (`1`–`16`) | HTML parcial · 404 JSON                              |
| POST    | `/mantenimiento/reportes/guardar_informe`         | Sí + CSRF | Valida y guarda el informe en JSON                                                                                                                                                                   | 201 JSON`{"id", "equipo", "mensaje"}` · 400 · 500 |
| GET     | `/mantenimiento/reportes/<id>/descargar`          | Sí        | Genera y descarga el PDF del reporte                                                                                                                                                                 | `application/pdf` · 404 · 422 · 500              |
| GET     | `/mantenimiento/reportes/<id>/descargar/<equipo>` | Sí        | Ruta anterior;`<equipo>` se ignora                                                                                                                                                                 | Igual que la anterior                                 |
| GET     | `/mantenimiento/historial?pagina=N`               | Sí        | Reportes guardados, 20 por página, del más reciente al más antiguo                                                                                                                                | HTML                                                  |
| GET     | `/<modulo>/<seccion>`                             | Sí        | Módulos del menú aún no implementados (`PROXIMAMENTE` en `app.py`)                                                                                                                            | HTML · 404                                           |

Todos los POST exigen el token CSRF (cabecera `X-CSRF-Token` o campo `csrf_token`). Las rutas con sesión
redirigen a `/login`, o devuelven 401 JSON si la petición es AJAX.

### Flujo de un informe de mantenimiento

```text
 Navegador                          Flask (app.py)                     informes.py / db.py          MySQL
 ─────────                          ──────────────                     ───────────────────          ─────
 1. POST /login ──────────────────▶ verifica usuario + hash ─▶ sesión
 2. GET /mantenimiento/reportes ──▶ forms_mantenimiento.html
 3. Elige "Protocolo" (forms.js)
    GET /load_form/Protocolo_de_mantenimiento ─▶ db.personal/clientes/marcas/servicios/tipo_equipos ─▶ SELECT catálogos
                                  ◀─ fragmento HTML con los <select> llenos
 4. Elige tipo de equipo
    GET /load_form/<1..16> ───────▶ protocolos_mantenimiento/<equipo>.html
 5. Clic en "Guardar" (forms.js)
    recolecta FormData → JSON {"tecnico.nombre", "cliente.nombre", "equipo.tipo",
                               "general.fecha_mantenimiento", ...campos del equipo}
    POST /…/guardar_informe ──────▶ _validar_informe() ─▶ db.subir_reporte() ─────────────▶ INSERT INTO reportes
                                  ◀─ 201 {"id": N}                                           (JSON completo en
 6. GET /…/N/descargar (app.js) ──▶ informes.generar_reporte_pdf(N)                           la columna reporte)
                                      ├─ db.obtener_reporte_por_id(N) ────────────────────▶ SELECT reporte
                                      ├─ tipo = data["equipo.tipo"] → TIPOS_EQUIPO
                                      ├─ contexto común (cliente, equipo, servicio, repuestos, técnico)
                                      │  + contexto del equipo (bomba, concentrador o genérico)
                                      ├─ render_template("pdf/<plantilla>.html")
                                      └─ WeasyPrint → bytes del PDF
                                  ◀─ reporte_N.pdf (descarga)
 7. Más tarde: GET /mantenimiento/historial → botón "Descargar PDF" → mismo paso 6
```

**Datos que se guardan.** La tabla `reportes` guarda `id_tecnico`, `id_cliente`, `id_equipo` y `fecha`
como columnas, y además todos los campos del formulario en la columna `reporte` (JSON). Las claves del
JSON son los atributos `name` de los campos (por ejemplo `equipo.modelo` o `protocolo_infusion.volumen_medio`).

**De dónde salen los nombres en el PDF.** El JSON guarda ids (técnico, cliente, marca, servicio). Al generar
el PDF, `informes.py` los traduce a nombres consultando los catálogos `personal`, `clientes`, `marcas` y
`mantenimientos`.

**Errores al descargar el PDF.** 404 si el reporte no existe o no tiene datos; 422 si el tipo de equipo no
tiene plantilla; 500 si falla WeasyPrint (por ejemplo, si faltan las librerías Pango; el detalle queda en el log).

## Cómo se generan los PDF

Ya **no** se usa Word ni LibreOffice. Cada informe es una plantilla HTML + CSS (Jinja2) que
se convierte a PDF con WeasyPrint, en memoria y sin archivos temporales.

| Tipo de equipo          | Plantilla                                                                                                                           |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| 2 – Bomba de infusión | `templates/pdf/bomba_infusion.html` (réplica del formato Word)                                                                   |
| 4 – Concentrador de O2 | `templates/pdf/concentrador_o2.html` (réplica del formato Word)                                                                  |
| Los demás (1-16)       | `templates/pdf/protocolo_generico.html`, construida leyendo el propio formulario de `templates/forms/protocolos_mantenimiento/` |

Piezas compartidas: `templates/pdf/base.html`, `_estilos.css` (colores, fuente Jost, pie de página)
y los parciales `_cliente`, `_equipo`, `_repuestos`, `_cierre`, etc. El código está en `informes/informes.py`.

El PDF toma el tipo de equipo del propio reporte guardado, no de la URL.

### Agregar o completar un equipo

1. Completa su formulario en `templates/forms/protocolos_mantenimiento/<equipo>.html`
   (títulos `<h3>` = secciones; el atributo `name` de cada campo es la clave que se guarda).
2. Listo: su PDF sale con la plantilla genérica. Si quieres un diseño propio (como bomba y concentrador),
   crea `templates/pdf/<equipo>.html`, una función `_contexto_<equipo>` en `informes.py` y apunta a ella en `TIPOS_EQUIPO`.
3. El código/versión del protocolo (PM-0xx) se edita en `TIPOS_EQUIPO`.

## Ejecutar en local en computadora

```bash
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # y complétalo
python tools/generar_hash.py                          # genera ADMIN_PASSWORD_HASH
python app.py
```

WeasyPrint necesita Pango. En Windows instala el runtime GTK (ver documentación de WeasyPrint);
en Linux: `sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0`.

## Pruebas

```bash
python -m unittest discover -s tests -t . -v
```

Usan una base de datos simulada; no necesitan MySQL. Cubren login, CSRF, validación, cabeceras,
cálculos del informe y el HTML de los 16 tipos de equipo. **No** generan el PDF final (eso lo hace WeasyPrint):
después de desplegar, descarga un PDF de cada equipo para revisar el diseño.

## Desplegar en Render

1. Sube el proyecto a un repositorio **sin** el `.env` (ya está en `.gitignore`).
2. Crea un *Web Service* con runtime **Docker** (o usa `render.yaml`).
3. Define las variables de entorno de `.env.example` en el panel de Render
   (`SECRET_KEY`, `ADMIN_USER`, `ADMIN_PASSWORD_HASH`, `db_*`, `TRUST_PROXY=1`, `FLASK_DEBUG=false`).
4. El chequeo de salud es `/healthz`.

La base de datos MySQL debe ser accesible desde Render; si tu proveedor exige TLS, define `db_ssl_ca`.

## Seguridad: qué se hizo

- Contraseña verificada con hash (`ADMIN_PASSWORD_HASH`, scrypt) y comparación en tiempo constante; ya no se compara texto plano.
- Bloqueo temporal tras 5 intentos fallidos de login por IP (en memoria, por proceso de gunicorn).
- Protección CSRF en todos los POST (formularios y AJAX); cierre de sesión por POST.
- Cookie de sesión `HttpOnly`, `SameSite=Lax` y `Secure` (en producción); sesión nueva al iniciar sesión; expira por inactividad.
- Cabeceras: Content-Security-Policy estricta (sin scripts inline), `X-Frame-Options`, `nosniff`, HSTS, `Cache-Control: no-store`.
- `SECRET_KEY` obligatoria en producción; `FLASK_DEBUG` apagado por defecto (antes estaba **encendido**).
- Validación del informe en el servidor (tipos, longitudes, fecha, tipo de equipo, tamaño máximo de 1 MB).
- Los errores ya no devuelven `str(e)` al navegador (podía filtrar datos de la base); se registran en el log.
- Consultas SQL parametrizadas con pool de conexiones; plantillas con autoescape (el contenido del usuario no se interpreta como HTML en el PDF).
- Contenedor Docker sin privilegios de root.

# SISEM — Sistema Integrado de Seguimiento de Equipos Médicos

Aplicación Flask para registrar mantenimientos de equipos biomédicos y descargar el informe en PDF.

## Cómo se generan los PDF

Ya **no** se usa Word ni LibreOffice. Cada informe es una plantilla HTML + CSS (Jinja2) que
se convierte a PDF con WeasyPrint, en memoria y sin archivos temporales.

| Tipo de equipo | Plantilla |
|---|---|
| 2 – Bomba de infusión | `templates/pdf/bomba_infusion.html` (réplica del formato Word) |
| 4 – Concentrador de O2 | `templates/pdf/concentrador_o2.html` (réplica del formato Word) |
| Los demás (1-16) | `templates/pdf/protocolo_generico.html`, construida leyendo el propio formulario de `templates/forms/protocolos_mantenimiento/` |

Piezas compartidas: `templates/pdf/base.html`, `_estilos.css` (colores, fuente Jost, pie de página)
y los parciales `_cliente`, `_equipo`, `_repuestos`, `_cierre`, etc. El código está en `informes/informes.py`.

El PDF toma el tipo de equipo del propio reporte guardado, no de la URL.

### Agregar o completar un equipo

1. Completa su formulario en `templates/forms/protocolos_mantenimiento/<equipo>.html`
   (títulos `<h3>` = secciones; el atributo `name` de cada campo es la clave que se guarda).
2. Listo: su PDF sale con la plantilla genérica. Si quieres un diseño propio (como bomba y concentrador),
   crea `templates/pdf/<equipo>.html`, una función `_contexto_<equipo>` en `informes.py` y apunta a ella en `TIPOS_EQUIPO`.
3. El código/versión del protocolo (PM-0xx) se edita en `TIPOS_EQUIPO`.

## Ejecutar en tu computador

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

Usan una base de datos simulada; no necesitan PostgreSQL. Cubren login, CSRF, validación, cabeceras,
cálculos del informe y el HTML de los 16 tipos de equipo. **No** generan el PDF final (eso lo hace WeasyPrint):
después de desplegar, descarga un PDF de cada equipo para revisar el diseño.

## Desplegar en Render

1. Sube el proyecto a un repositorio **sin** el `.env` (ya está en `.gitignore`).
2. Crea un *Web Service* con runtime **Docker** (o usa `render.yaml`).
3. Define las variables de entorno de `.env.example` en el panel de Render
   (`SECRET_KEY`, `ADMIN_USER`, `ADMIN_PASSWORD_HASH`, `DATABASE_URL`, `TRUST_PROXY=1`, `FLASK_DEBUG=false`).
4. El chequeo de salud es `/healthz`.

La base de datos es **PostgreSQL**. En Render define `DATABASE_URL` con la *Internal Database URL* de tu base.
La guía para migrar desde MariaDB (esquema, copia de datos con DBeaver y verificación) está en `database/postgres/LEEME.md`.

## Seguridad: qué se hizo

- Contraseña verificada con hash (`ADMIN_PASSWORD_HASH`, scrypt) y comparación en tiempo constante; ya no se compara texto plano.
- Bloqueo temporal tras 5 intentos fallidos de login por IP (en memoria, por proceso de gunicorn).
- Protección CSRF en todos los POST (formularios y AJAX); cierre de sesión por POST.
- Cookie de sesión `HttpOnly`, `SameSite=Lax` y `Secure` (en producción); sesión nueva al iniciar sesión; expira por inactividad.
- Cabeceras: Content-Security-Policy estricta (sin scripts inline), `X-Frame-Options`, `nosniff`, HSTS, `Cache-Control: no-store`.
- `SECRET_KEY` obligatoria en producción; `FLASK_DEBUG` apagado por defecto (antes estaba **encendido**).
- Validación del informe en el servidor (tipos, longitudes, fecha, tipo de equipo, tamaño máximo de 1 MB).
- Los errores ya no devuelven `str(e)` al navegador (podía filtrar datos de la base); se registran en el log.
- Consultas SQL parametrizadas con pool de conexiones (PostgreSQL); plantillas con autoescape (el contenido del usuario no se interpreta como HTML en el PDF).
- Contenedor Docker sin privilegios de root.

## Pendiente por tu parte

- **Rota las credenciales** que estaban en el `.env` compartido (contraseña de la base de datos, `SECRET_KEY`, usuario/clave de acceso).
- Si en algún momento subiste ese `.env` a un repositorio, considéralo comprometido.
- Hay un solo usuario administrador definido por entorno. Si necesitas varios usuarios o roles, hay que crear una tabla de usuarios.
- Los límites de intentos de login son por proceso; con 2 workers el máximo real es de 10 intentos.

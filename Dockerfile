FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Librerías del sistema que necesita WeasyPrint para dibujar el PDF
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libharfbuzz-subset0 \
        libffi8 shared-mime-info fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# No se ejecuta como root
RUN useradd --create-home --uid 10001 sisem && chown -R sisem /app
USER sisem

ENV PORT=10000 \
    TRUST_PROXY=1
EXPOSE 10000

# Render define $PORT. El tiempo límite alto cubre PDFs grandes.
CMD gunicorn app:sisem --bind 0.0.0.0:${PORT} --workers 2 --timeout 120 --access-logfile -

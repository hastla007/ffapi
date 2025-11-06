FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libjpeg-dev \
    zlib1g-dev \
    libpng-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy both the app and fastapi_csrf_protect directories
COPY app ./app
COPY fastapi_csrf_protect ./fastapi_csrf_protect

# volumes for outputs and work
VOLUME ["/data/public", "/data/work", "/data/logs"]
ENV PUBLIC_DIR=/data/public
ENV WORK_DIR=/data/work
ENV LOGS_DIR=/data/logs
ENV RETENTION_DAYS=7
ENV PUBLIC_BASE_URL=

EXPOSE 3000

# Health check - ensures the service is responding
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3000/health').read()" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000"]

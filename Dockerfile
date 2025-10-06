FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Copy logo - ensure logo-ffapi.png exists in the same directory as Dockerfile
COPY logo-ffapi.png /app/app/logo-ffapi.png

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

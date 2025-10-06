
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# volumes for outputs and work
VOLUME ["/data/public", "/data/work"]
ENV PUBLIC_DIR=/data/public
ENV WORK_DIR=/data/work
ENV RETENTION_DAYS=7
ENV PUBLIC_BASE_URL=

EXPOSE 3000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000"]

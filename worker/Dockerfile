# ClipForge AI worker — Python 3.12
#
# The worker is the ONLY service that touches video processing (downloads,
# transcription, analysis, FFmpeg rendering). It never runs inside Vercel.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Storage lives on a volume so rendered clips survive container restarts.
RUN useradd --create-home appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data
ENV WORKER_STORAGE_ROOT=/data
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

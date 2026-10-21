# The ticket analysis API for Azure Container Apps. The image holds code only, including the taxonomy and the
# alert-name table in ticket_analysis/. The model files and the directory snapshots are client data and come
# from the private Blob container at startup (MODEL_STORE, DIRECTORY_STORE) or, locally, from mounted folders
# (docker-compose.yml).
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt requirements-api.txt ./
RUN pip install -r requirements-api.txt

COPY ticket_analysis/ ticket_analysis/
COPY api/ api/

# A non-root user; /models is a writable cache for files downloaded from MODEL_STORE
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin api \
    && mkdir -p /models \
    && chown 10001:10001 /models
USER 10001

ENV MODEL_DIR=/models \
    API_HOST=0.0.0.0 \
    API_PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

CMD ["python", "-m", "api"]

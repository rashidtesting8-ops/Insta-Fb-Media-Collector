# Phase 1 media worker skeleton.
# Non-root user, healthcheck and port conventions adapted from the legacy
# builder Dockerfile (reference/legacy-extracted/pet-gromming-agent-factory-main/Dockerfile).
# Chromium/Playwright is deliberately deferred to Phase 2 to keep this skeleton light.
FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/srv

# ffmpeg for Phase 2 frame extraction; ca-certificates for outbound HTTPS.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --shell /bin/bash appuser
COPY app ./app
COPY tests ./tests
RUN chown -R appuser:appuser /srv
USER appuser

EXPOSE 8787
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3)"

CMD ["uvicorn", "app.service:app", "--host", "0.0.0.0", "--port", "8787", "--no-access-log", "--no-proxy-headers"]

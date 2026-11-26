# Pet Grooming Media Worker (V2.1)
# Non-root runtime, ffmpeg + libsodium23 (Argon2id named auth), and reproducible pytest.
FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/srv \
    TMPDIR=/tmp

# ffmpeg for video frame extraction; ca-certificates for HTTPS; libsodium23 for Argon2id named auth.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates libsodium23 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Optional bounded Playwright + Chromium JS fallback (disabled by default for lean image/CI).
# Enable via: docker compose build --build-arg INSTALL_PLAYWRIGHT=1 collector
ARG INSTALL_PLAYWRIGHT=0
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN if [ "$INSTALL_PLAYWRIGHT" = "1" ]; then \
        pip install --no-cache-dir "playwright>=1.49,<2" \
        && playwright install --with-deps chromium \
        && rm -rf /var/lib/apt/lists/*; \
    fi

RUN useradd --create-home --shell /bin/bash appuser \
 && mkdir -p /data/projects /srv/docs \
 && if [ -d /ms-playwright ]; then chown -R appuser:appuser /ms-playwright; fi

COPY pyproject.toml ./pyproject.toml
COPY app ./app
COPY tests ./tests
COPY scripts/named_bootstrap.py ./scripts/named_bootstrap.py
COPY scripts/sqlite_backup.py ./scripts/sqlite_backup.py
COPY scripts/readiness_probe.py ./scripts/readiness_probe.py
RUN chown -R appuser:appuser /srv /data/projects
USER appuser

EXPOSE 8787
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/ready', timeout=3)"

CMD ["uvicorn", "app.service:app", "--host", "0.0.0.0", "--port", "8787", "--no-access-log", "--no-proxy-headers"]

FROM python:3.12-slim

LABEL org.opencontainers.image.title="garmin-nostra"
LABEL org.opencontainers.image.description="Multi-user Garmin Connect sync — CalDAV + Mastodon"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (UID 1000 matches common desktop UID for bind-mount compat)
RUN groupadd -r -g 1000 appuser && useradd -r -u 1000 -g appuser -s /bin/false appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Bind-mount from host: persists DB, GPX files, map images, and auth tokens
VOLUME ["/data"]

USER appuser

ENTRYPOINT ["/entrypoint.sh"]

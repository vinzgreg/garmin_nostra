FROM python:3.12-slim

LABEL org.opencontainers.image.title="garmin-nostra"
LABEL org.opencontainers.image.description="Multi-user Garmin Connect sync — CalDAV + Mastodon"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Bind-mount from host: persists DB, GPX files, map images, and auth tokens
VOLUME ["/data"]

ENTRYPOINT ["/entrypoint.sh"]

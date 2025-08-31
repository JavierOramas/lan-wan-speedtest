FROM python:3.11-slim

# System deps (incl. CA certs & ping for diagnostics)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini ca-certificates curl iputils-ping && \
    rm -rf /var/lib/apt/lists/*

# Python deps
# - speedtest-cli installs both the CLI and the python module `speedtest`
RUN pip install --no-cache-dir flask gunicorn SQLAlchemy requests speedtest-cli

# App
WORKDIR /app
COPY app.py /app/app.py
COPY templates /app/templates

# Data dir for SQLite
RUN mkdir -p /data
ENV DB_PATH=/data/netspeed.sqlite
ENV PORT=8080

# Scheduling (default every 30 minutes); set to "off" to disable
ENV WAN_SCHEDULE_CRON="*/30 * * * *"

# Public URL for Telegram link (set this at runtime to your host/IP)
# e.g. -e PAGE_URL="http://192.168.0.198:8080"
ENV PAGE_URL=""

# Web interface URL for Telegram messages (set this at runtime)
# e.g. -e WEB_URL="http://192.168.0.198:8080"
ENV WEB_URL="http://localhost:8080"

# Telegram Bot Configuration (set these at runtime)
# -e TELEGRAM_BOT_TOKEN="your_bot_token_here"
# -e TELEGRAM_CHAT_ID="your_chat_id_here"
# -e TELEGRAM_TOPIC_ID="your_topic_id_here"  # Optional: for forum topics
# e.g. -e TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrsTUVwxyz" -e TELEGRAM_CHAT_ID="123456789"

# Gunicorn: 2 workers so UI stays responsive during speed tests
ENV GUNICORN_CMD_ARGS="--threads=8 --workers=2 --timeout=180 --graceful-timeout=30"

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "-b", "0.0.0.0:8080", "app:app"]

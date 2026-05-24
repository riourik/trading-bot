FROM python:3.12-slim

WORKDIR /app

# Git pour le clone au démarrage + curl pour le healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Dépendances Python dans l'image (rapide au restart)
RUN pip install --no-cache-dir \
    yfinance \
    requests \
    apscheduler \
    pytz \
    numpy \
    python-dotenv

# Entrypoint : rm /tmp/bot + git clone frais + démarrage
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Dossier logs persistant
RUN mkdir -p /app/logs

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD pgrep -f "python.*main" || exit 1

CMD ["/entrypoint.sh"]

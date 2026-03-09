FROM python:3.12-slim

# Playwright needs these system deps for chromium
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libxshmfence1 libx11-xcb1 libxcb1 \
    fonts-liberation fonts-noto-color-emoji curl && \
    rm -rf /var/lib/apt/lists/*

# Install supercronic (cron léger pour Docker)
ARG SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-amd64
ARG SUPERCRONIC_SHA1SUM=71b0d58cc53f6bd72cf2f293e09e294b79c666d8
RUN curl -fsSLO "$SUPERCRONIC_URL" && \
    echo "$SUPERCRONIC_SHA1SUM  supercronic-linux-amd64" | sha1sum -c - && \
    chmod +x supercronic-linux-amd64 && \
    mv supercronic-linux-amd64 /usr/local/bin/supercronic

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install --with-deps chromium

COPY src/ src/
COPY planner_config.json .

# Crontab : tous les mardis à 11h00
RUN echo '0 11 * * 2 python /app/src/main.py' > /app/crontab

CMD ["supercronic", "/app/crontab"]

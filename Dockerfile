FROM python:3.12-slim

# Playwright needs these system deps for chromium
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libxshmfence1 libx11-xcb1 libxcb1 \
    fonts-liberation fonts-noto-color-emoji && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install --with-deps chromium

COPY src/ src/
# Configuration par défaut, remplacée par le volume planner_config.json
COPY planner_config.example.json planner_config.json

EXPOSE 8000

# Interface web + lancement automatique du pipeline
CMD ["uvicorn", "webapp.app:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]

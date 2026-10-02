FROM python:3.12-slim

# ffmpeg for yt-dlp audio extraction; the rest are runtime libs for the Camoufox browser.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2 \
        libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 \
        libgbm1 libpango-1.0-0 libcairo2 libatk1.0-0 \
        libatk-bridge2.0-0 libxkbcommon0 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download the Camoufox browser binary at build time so first render is fast.
RUN python -m camoufox fetch --browserforge || true

COPY app ./app

ENV PYTHONUNBUFFERED=1
EXPOSE 5000

CMD ["python", "-m", "app.server"]

FROM python:3.14-slim

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

# camoufox 0.3.9 breaks on newer browser builds ("Unknown property navigator.appCodeName"),
# so replace the just-fetched latest build with a pinned one.
ARG CAMOUFOX_BROWSER=152.0.4-beta.30
RUN python -c "import io,json,os,shutil,urllib.request,zipfile; v='${CAMOUFOX_BROWSER}'; d='/root/.cache/camoufox'; \
u='https://github.com/daijro/camoufox/releases/download/v%s/camoufox-%s-lin.x86_64.zip'%(v,v); \
shutil.rmtree(d,ignore_errors=True); os.makedirs(d); \
zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(u).read())).extractall(d); \
ver,rel=v.split('-',1); json.dump({'version':ver,'release':rel},open(d+'/version.json','w'))" \
    && chmod -R 755 /root/.cache/camoufox \
    && python -m camoufox version

COPY app ./app

ENV PYTHONUNBUFFERED=1
EXPOSE 5000

CMD ["python", "-m", "app.server"]

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY tgmd/ ./tgmd/
COPY pikpak_wms/ ./pikpak_wms/
COPY config/ ./config/

# Runtime state lives on one volume so sessions, the database and downloads
# all survive a container rebuild.
ENV SESSION_DIR=/data/sessions \
    DATA_DIR=/data/db \
    DOWNLOAD_DIR=/data/downloads

RUN useradd --create-home --uid 10001 tgmd \
    && mkdir -p /data/sessions /data/db /data/downloads \
    && chown -R tgmd:tgmd /data /app

USER tgmd

# Only needed when HTTP_ENABLED=true for Telegram-to-PikPak transfers.
EXPOSE 8080

CMD ["python", "-m", "tgmd"]

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY essl_hub ./essl_hub
COPY templates ./templates
COPY static ./static
COPY diagnose.py .

# Fixed UID/GID 10001: every service runs as this identity, so each can read
# and write the database, spool and status files in the shared /data
# folder. A fresh named volume copies /data's ownership from the image.
RUN groupadd --system --gid 10001 essl \
    && useradd --system --uid 10001 --gid 10001 --create-home --home-dir /home/essl essl \
    && mkdir -p /data \
    && chown 10001:10001 /data
USER essl

ENV PYTHONUNBUFFERED=1 \
    ESSL_DB_PATH=/data/essl.db

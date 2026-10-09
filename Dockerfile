# Immagine ufficiale Python dal mirror pubblico AWS: evita i limiti di download anonimi di Docker Hub
FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

# UID/GID 99:100 = nobody:users, i default di Unraid per /mnt/user/appdata
ARG PUID=99
ARG PGID=100

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    TZ=Europe/Rome

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && (getent group ${PGID} || groupadd -g ${PGID} app) \
    && useradd -u ${PUID} -g ${PGID} -M -d /app -s /usr/sbin/nologin app 2>/dev/null || true \
    && mkdir -p /data && chown ${PUID}:${PGID} /data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY create_consumer_key.py .

USER ${PUID}:${PGID}
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-m", "app.healthcheck"]

CMD ["python", "-m", "app.main"]

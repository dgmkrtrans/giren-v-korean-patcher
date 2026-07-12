FROM node:24-slim AS frontend

WORKDIR /build/webtool/frontend
COPY webtool/frontend/package*.json ./
RUN npm ci
COPY webtool/frontend ./
RUN npm run build


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul \
    WEBTOOL_DATABASE_URL=mysql://webtool:webtool@mysql:3306/gihren_webtool?charset=utf8mb4

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates cron g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY webtool/backend /app/webtool/backend
COPY webtool/server.py /app/webtool/server.py
COPY scripts /app/scripts
COPY patch_data /app/patch_data
RUN mkdir -p /app/results
COPY --from=frontend /build/webtool/frontend/dist /app/webtool/frontend/dist

COPY webtool/docker/backup-textures-manifest /usr/local/bin/backup-textures-manifest
COPY webtool/docker/webtool-entrypoint.sh /usr/local/bin/webtool-entrypoint.sh
COPY webtool/docker/textures-manifest-backup.cron /etc/cron.d/textures-manifest-backup

RUN mkdir -p /data \
    && chmod 0755 /usr/local/bin/backup-textures-manifest /usr/local/bin/webtool-entrypoint.sh \
    && chmod 0644 /etc/cron.d/textures-manifest-backup \
    && crontab /etc/cron.d/textures-manifest-backup

EXPOSE 8765

ENTRYPOINT ["/usr/local/bin/webtool-entrypoint.sh"]
CMD ["python", "webtool/server.py", "--host", "0.0.0.0", "--port", "8765"]

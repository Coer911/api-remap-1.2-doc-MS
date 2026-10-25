FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    TZ=Europe/Moscow \
    HUB_DATABASE_URL=sqlite+aiosqlite:////data/hub.sqlite3

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY hub ./hub
COPY config.yaml .

# база SQLite; на хостинге сюда монтируется постоянный диск
RUN mkdir -p /data
VOLUME /data

EXPOSE 8080
# роль — из HUB_ROLE (по умолчанию all)
CMD ["python", "-m", "hub", "serve"]

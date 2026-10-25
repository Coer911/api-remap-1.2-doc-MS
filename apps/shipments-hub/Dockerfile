FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    HUB_DB=/data/hub.sqlite3 \
    TZ=Europe/Moscow

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY hub ./hub
COPY config.yaml .

# база хаба; на хостинге сюда монтируется постоянный диск
RUN mkdir -p /data
VOLUME /data

EXPOSE 8080
CMD ["python", "-m", "hub", "serve"]

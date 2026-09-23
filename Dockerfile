FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN test -f /app/assets/info-preview.png
RUN test -f /app/assets/report-poster-placeholder-v1.png
RUN test -f /app/assets/report-poster-placeholder-v2.jpg
RUN set -eu; \
    for asset in \
        home-v1.jpg \
        subscription-v1.jpg \
        stats-v1.jpg \
        lists-v1.jpg \
        lists-anime-v1.jpg \
        lists-manga-v1.jpg \
        lists-ranobe-v1.jpg \
        owner-v1.jpg \
        owner-backup-v1.jpg \
        owner-broadcast-v1.jpg; \
    do \
        test -f "/app/assets/main-menu/$asset"; \
    done; \
    test "$(find /app/assets/main-menu -maxdepth 1 -type f -name '*.jpg' | wc -l)" -eq 10
RUN test -f /app/examples/facts.json

# Порт healthcheck-сервера (healthcheck.py). Платформы, читающие Dockerfile,
# по EXPOSE понимают, на какой порт направлять пробу /health. Если хостинг
# инъецирует свой $PORT — healthcheck.py его подхватит (EXPOSE здесь как
# дефолт/документация и не мешает).
EXPOSE 8080

CMD ["python", "main.py"]

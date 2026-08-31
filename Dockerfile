FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN test -f /app/assets/info-preview.png
RUN test -f /app/examples/facts.json

# Порт healthcheck-сервера (healthcheck.py). Платформы, читающие Dockerfile,
# по EXPOSE понимают, на какой порт направлять пробу /health. Если хостинг
# инъецирует свой $PORT — healthcheck.py его подхватит (EXPOSE здесь как
# дефолт/документация и не мешает).
EXPOSE 8080

CMD ["python", "main.py"]

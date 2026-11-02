# The kiosk relay (see server/README.md). Build context: the repo root (docker-compose.yml).
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY server/app.py pi/control.html ./
USER 1000:1000
EXPOSE 8000
CMD ["python3", "app.py"]

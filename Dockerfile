FROM python:3.11-slim

WORKDIR /app

# Логи сразу в поток (иначе docker logs пустые) и без .pyc
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Копируем зависимости и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем всё приложение
COPY . .

# Создаём папки для отчётов, БД и логов
RUN mkdir -p /app/reports /app/data /app/logs

# Порт приложения
EXPOSE 5000

# Проверка живости (docker сам перезапустит unhealthy-контейнер при restart: always)
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4)"

# Продакшн-сервер.
# ВАЖНО: ровно ОДИН воркер — фоновый дозор отчётов и синхронизация с Firebase
# должны идти в единственном экземпляре, а SQLite не любит параллельную запись
# из разных процессов. Параллелизм дают потоки.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", \
     "--workers", "1", "--threads", "8", \
     "--timeout", "120", "--graceful-timeout", "30", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "wsgi:application"]

FROM python:3.11-slim

WORKDIR /app

# Копируем зависимости и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем всё приложение
COPY . .

# Создаём папки для reports и данных (если не существуют)
RUN mkdir -p /app/reports

# Порт Flask
EXPOSE 5000

# Точка входа — run.py
CMD ["python", "run.py"]

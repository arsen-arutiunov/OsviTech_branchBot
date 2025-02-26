FROM python:3.10-slim

# Создаём рабочую директорию внутри контейнера
WORKDIR /app

# Скопируем список зависимостей и установим их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем остальной код бота
COPY . .

# Запускаем бот
CMD ["python", "bot.py"]

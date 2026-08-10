FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    ca-certificates \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]

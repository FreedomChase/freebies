FROM python:3.12-slim

WORKDIR /app

# Pillow / qrcode runtime libs
RUN apt-get update \
 && apt-get install -y --no-install-recommends libjpeg62-turbo zlib1g \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persist SQLite DB outside the image
ENV FREEBIES_DB_PATH=/data/freebies.db
RUN mkdir -p /data

EXPOSE 4003

CMD ["python", "app.py"]

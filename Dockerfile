FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    FREEBIES_DB_PATH=/data/freebies.db

WORKDIR /app

# Hash-locked, wheels only: nothing is built or fetched unverified.
COPY requirements.lock.txt .
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.lock.txt

COPY app.py .
COPY templates templates
COPY static static

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin freebies \
 && mkdir -p /data && chown freebies /data
USER freebies
VOLUME /data

EXPOSE 4003

# One worker: the rate limiter lives in process memory. Threads carry the concurrency.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:4003", "--access-logfile", "-", "app:app"]

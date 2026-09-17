FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py .
COPY templates templates

RUN useradd --create-home --uid 1000 tracker
USER tracker

EXPOSE 8000
# Create tables + admin on boot, then serve. 1 worker x 4 threads keeps RAM ~50 MB.
CMD ["sh", "-c", "flask --app app init-db && exec gunicorn --workers 1 --threads 4 --bind 0.0.0.0:8000 --access-logfile - app:app"]

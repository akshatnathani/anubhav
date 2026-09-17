FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py gunicorn.conf.py ./
COPY templates templates

RUN useradd --create-home --uid 1000 tracker
USER tracker

EXPOSE 8000
# Create tables + admin on boot, then serve (gunicorn.conf.py also starts the email loop).
CMD ["sh", "-c", "flask --app app init-db && exec gunicorn -c gunicorn.conf.py app:app"]

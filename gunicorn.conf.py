import logging

bind = "0.0.0.0:8000"
workers = 1          # small VM: one process, a few threads (~50 MB RAM)
threads = 4
accesslog = "-"


def post_worker_init(worker):
    from app import app, start_mailer

    app.logger.setLevel(logging.INFO)
    app.logger.handlers = worker.log.error_log.handlers
    start_mailer()

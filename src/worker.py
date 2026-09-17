"""Cloudflare Worker entrypoint: Flask handles HTTP, the cron trigger sends reminders."""
from workers import WorkerEntrypoint, wsgi

from app import app, configure, run_scheduled_jobs


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        configure(self.env)
        return await wsgi.fetch(app, request, self.env)

    async def scheduled(self, controller, env, ctx):
        configure(self.env)
        run_scheduled_jobs()

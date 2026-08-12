"""Punto de entrada para el servidor de producción.

Sirve la interfaz web en / y la API REST en /api/, ambas sobre la misma
instancia del servicio: lo que cargás por pantalla lo ve la API y viceversa.

IMPORTANTE: el estado vive en memoria (ver README). Correr con un solo worker.
    gunicorn wsgi:app --workers 1 --threads 4
"""
from __future__ import annotations

import os

from app.api.main import create_app as create_api_app
from app.web.main import create_web_app
from seed.demo import bootstrap

service, budget, version = bootstrap()

web_app = create_web_app(service, budget.id)
api_app = create_api_app(service)


class Router:
    """Enruta /api/* a la API y el resto a la interfaz web."""

    def __init__(self, web, api):
        self.web, self.api = web, api

    def __call__(self, environ, start_response):
        if environ.get("PATH_INFO", "").startswith("/api/"):
            return self.api(environ, start_response)
        return self.web(environ, start_response)


app = Router(web_app, api_app)

if __name__ == "__main__":
    from werkzeug.serving import run_simple
    run_simple("0.0.0.0", int(os.environ.get("PORT", 8000)), app)

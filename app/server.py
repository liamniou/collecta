"""Container entrypoint: init DB, start the worker, serve the app."""
from waitress import serve

from .main import app, start_worker
from .models import init_db


def run() -> None:
    init_db()
    start_worker()
    serve(app, host="0.0.0.0", port=5000, threads=8)


if __name__ == "__main__":
    run()

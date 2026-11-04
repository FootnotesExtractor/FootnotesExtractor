#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Production entrypoint. Serves the Flask app via waitress (a production-grade
WSGI server, pure Python — works the same on Linux or Windows) instead of
the Werkzeug development server `python app.py` uses.

Importing `app` from app.py runs all of that module's real initialization
(db.init_db(), blueprint registration, secret-key loading, stuck-job
recovery) exactly as `python app.py` does — this file only changes *how*
the WSGI app is served, not what gets set up first.

Usage:
    python wsgi.py                    # binds 0.0.0.0:5002, 8 threads
    python wsgi.py --host 127.0.0.1   # bind loopback only (put a reverse
                                       # proxy like Caddy in front — see
                                       # deploy/DEPLOY.md)
    python wsgi.py --port 8080 --threads 16

`app` is also importable directly as a plain WSGI callable (`wsgi:app`) if
you'd rather run this under gunicorn or another WSGI server on Linux
instead of waitress.
"""

import argparse
import os
import threading

from app import app, _trigger_self_warmup  # noqa: F401 — import runs app.py's module-level init


def main():
    parser = argparse.ArgumentParser(description="Serve the app in production via waitress")
    parser.add_argument('--host', default='0.0.0.0',
                         help="Bind address. Use 127.0.0.1 if a reverse proxy (Caddy/nginx) "
                              "in front of this is what's actually facing the network.")
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', 5002)))
    parser.add_argument('--threads', type=int, default=8,
                         help="waitress worker threads (each request handler runs in one; "
                              "the model-inference pipeline itself still runs on the app's own "
                              "background worker thread, same as under the dev server)")
    args = parser.parse_args()

    # Same model-preloading trick app.py's own __main__ block uses: poll
    # until the server answers, then fire one request at /api/_internal/warmup
    # so the first real user isn't the one who pays for a cold model load.
    threading.Thread(target=_trigger_self_warmup, kwargs={'port': args.port}, daemon=True).start()

    from waitress import serve
    print(f"🚀 Serving (production/waitress) on http://{args.host}:{args.port} — {args.threads} threads")
    serve(app, host=args.host, port=args.port, threads=args.threads)


if __name__ == '__main__':
    main()

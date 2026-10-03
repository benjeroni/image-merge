"""Public image endpoint + staff UI.

GET /<slug>.jpg?name=Ben  -> personalized image (public, what emails load)
/admin                     -> staff UI (see admin.py)

Privacy: recipient names only exist in the request and in memory while drawing.
There's no access log (gunicorn's is off by default, and Flask's error log records the path, not the query).
"""
import os
import secrets
from datetime import timedelta

from flask import Flask, abort, redirect, request, url_for

import admin
import core


def secret_key():
    """One key shared by all workers, created on first start. Link-based so two workers can't race."""
    path = core.DATA / "secret_key"
    if not path.exists():
        tmp = core.DATA / f".secret_key-{os.getpid()}"
        tmp.write_text(secrets.token_hex(32))
        tmp.chmod(0o600)
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
        tmp.unlink()
    return path.read_text()


core.seed_sample()

app = Flask(__name__)
app.config.update(
    SECRET_KEY=secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") != "0",  # set COOKIE_SECURE=0 only for plain-http testing
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=15 * 1024 * 1024,
)
app.register_blueprint(admin.bp)


@app.get("/<slug>.jpg")
def image(slug):
    body = core.render_live(slug, request.args.get("name"))
    if body is None:
        abort(404)
    # ponytail: cached for a year by CDNs/Gmail; live templates are immutable, so that's safe.
    return body, 200, {"Content-Type": "image/jpeg", "Cache-Control": "public, max-age=31536000"}


@app.get("/")
def home():
    return redirect(url_for("admin.index"))


@app.get("/healthz")
def healthz():
    templates, errors = core.live()
    if errors:
        return {"templates": sorted(templates), "rejected": errors}, 500
    return {"templates": sorted(templates)}

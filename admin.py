"""Staff web UI at /admin: accounts, template editor, preview, publish.

Create the first admin from the server:
  docker exec -it image-merge python admin.py add-user ben --admin
"""
import getpass
import html
import os
import re
import secrets
import sqlite3
import sys
import time
from contextlib import closing
from functools import wraps

from flask import (Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import core

bp = Blueprint("admin", __name__, url_prefix="/admin", template_folder="pages")

DB = core.DATA / "users.db"
MIN_PASSWORD = 10
MAX_FAILURES, LOCKOUT_SECONDS = 5, 15 * 60
DUMMY_HASH = generate_password_hash("timing-equalizer")


def query(sql, *args):
    with closing(sqlite3.connect(DB)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, args).fetchall()
        con.commit()
        return rows


query("""CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY, password TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0)""")
# Archiving only hides a template from the main lists; archived live templates keep serving sent emails.
query("CREATE TABLE IF NOT EXISTS archived (slug TEXT PRIMARY KEY, archived_by TEXT NOT NULL, archived_at REAL NOT NULL)")


def add_user(username, password, is_admin=False):
    """Returns an error message, or None."""
    if not core.SLUG.fullmatch(username):
        return "Usernames are lowercase letters, digits and dashes."
    if len(password) < MIN_PASSWORD:
        return f"Passwords must be at least {MIN_PASSWORD} characters."
    try:
        query("INSERT INTO users (username, password, is_admin) VALUES (?, ?, ?)",
              username, generate_password_hash(password), int(is_admin))
    except sqlite3.IntegrityError:
        return f"{username} already exists."


def set_password(username, password):
    if len(password) < MIN_PASSWORD:
        return f"Passwords must be at least {MIN_PASSWORD} characters."
    query("UPDATE users SET password = ?, failures = 0, locked_until = 0 WHERE username = ?",
          generate_password_hash(password), username)


# ---------- auth ----------

@bp.before_request
def require_login():
    user = session.get("user")
    rows = query("SELECT username, is_admin FROM users WHERE username = ?", user) if user else []
    g.user = rows[0] if rows else None  # re-read every request so removed users lose access at once
    if request.method == "POST" and request.endpoint != "admin.login":
        sent = request.form.get("csrf") or request.headers.get("X-CSRF", "")
        if not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Your session expired; reload the page and try again.")
    if g.user is None and request.endpoint != "admin.login":
        return redirect(url_for("admin.login"))


@bp.after_request
def headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def admin_only(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        rows = query("SELECT * FROM users WHERE username = ?", username)
        user = rows[0] if rows else None
        if user and user["locked_until"] > time.time():
            flash("Too many failed attempts. Try again in 15 minutes.")
        elif check_password_hash(user["password"] if user else DUMMY_HASH, password) and user:
            query("UPDATE users SET failures = 0, locked_until = 0 WHERE username = ?", username)
            session.clear()
            session.permanent = True
            session.update(user=username, csrf=secrets.token_urlsafe(32))
            return redirect(url_for("admin.index"))
        else:
            if user:
                failures = user["failures"] + 1
                locked = time.time() + LOCKOUT_SECONDS if failures >= MAX_FAILURES else 0
                query("UPDATE users SET failures = ?, locked_until = ? WHERE username = ?",
                      0 if locked else failures, locked, username)
            flash("Wrong username or password.")
    return render_template("login.html")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin.login"))


@bp.route("/account", methods=["GET", "POST"])
def account():
    if request.method == "POST":
        rows = query("SELECT password FROM users WHERE username = ?", g.user["username"])
        if not check_password_hash(rows[0]["password"], request.form.get("current", "")):
            flash("Current password is wrong.")
        else:
            flash(set_password(g.user["username"], request.form.get("new", "")) or "Password changed.")
    return render_template("account.html")


@bp.route("/users", methods=["GET", "POST"])
@admin_only
def users():
    if request.method == "POST":
        action, username = request.form.get("action"), request.form.get("username", "").strip().lower()
        if action == "add":
            flash(add_user(username, request.form.get("password", ""), bool(request.form.get("is_admin")))
                  or f"Added {username}.")
        elif action == "reset":
            flash(set_password(username, request.form.get("password", "")) or f"Password reset for {username}.")
        elif action == "delete" and username != g.user["username"]:
            query("DELETE FROM users WHERE username = ?", username)
            flash(f"Removed {username}.")
        return redirect(url_for("admin.users"))
    return render_template("users.html", users=query("SELECT username, is_admin FROM users ORDER BY username"))


# ---------- templates ----------

def public_base():
    return (os.environ.get("PUBLIC_URL") or request.url_root).rstrip("/")


def next_free(slug):
    """Suggested name for a copy: fall-orientation -> fall-orientation-2 (or -3, ...)."""
    base = re.sub(r"-\d+$", "", slug)[:45]
    return next(f"{base}-{n}" for n in range(2, 10_000) if not core.slug_taken(f"{base}-{n}"))


@bp.get("/")
def index():
    archived = {r["slug"] for r in query("SELECT slug FROM archived")}
    drafts = [p.stem for p in sorted(core.DRAFTS.glob("*.json"))]
    live = [(p.stem, core.read_json(p).get("meta") or {}) for p in sorted(core.LIVE.glob("*.json"))]
    return render_template(
        "index.html", rejected=core.live()[1], next_free=next_free,
        drafts=[s for s in drafts if s not in archived],
        live=[(s, m) for s, m in live if s not in archived],
        archived=[(s, "draft") for s in drafts if s in archived] + [(s, "live") for s, _ in live if s in archived])


@bp.post("/new")
def new():
    slug = request.form.get("slug", "").strip().lower()
    if not core.SLUG.fullmatch(slug):
        flash("Names are lowercase letters, digits and dashes, e.g. fall-orientation.")
    elif core.slug_taken(slug):
        flash(f"{slug} is already used. Pick another name.")
    else:
        try:
            image = core.store_image(request.files["image"].stream)
        except (KeyError, ValueError) as e:
            flash(str(e) if isinstance(e, ValueError) else "Choose a background image.")
        else:
            fields = core.new_draft_fields(image)
            fields["meta"] = {"created_by": g.user["username"]}
            core.save_json(core.DRAFTS / f"{slug}.json", fields)
            if warning := size_warning(image):
                flash(warning)
            return redirect(url_for("admin.edit", slug=slug))
    return redirect(url_for("admin.index"))


def size_warning(image):
    """Uploads are shown at half size; warn when that's narrower than a full-width email."""
    w, _ = core.image_size(image)
    if w < core.IMAGE_WIDTH[1]:
        return (f"This image will appear {w // 2}px wide in the email. For a full-width ({core.IMAGE_WIDTH[1] // 2}px) "
                f"banner, upload a version {core.IMAGE_WIDTH[1]}px wide.")


def draft(slug):
    path = core.DRAFTS / f"{slug}.json"
    if not core.SLUG.fullmatch(slug) or not path.exists():
        abort(404)
    return path, core.read_json(path)


def fields_from_form(form, saved):
    """Editor form -> template fields. Bad numbers pass through as text so validate() can report them."""
    def num(key, cast=int):
        try:
            return cast(form.get(key, ""))
        except ValueError:
            return form.get(key)
    fields = {"image": saved["image"], "font": form.get("font", ""), "message": form.get("message", "").strip(),
              "fallback": form.get("fallback", "").strip(), "box": [num(k) for k in ("x0", "y0", "x1", "y1")],
              "align": form.get("align"), "valign": form.get("valign"), "color": form.get("color", "").upper(),
              "min_size": num("min_size"), "max_size": num("max_size"),
              "line_spacing": num("line_spacing", float), "meta": saved.get("meta")}
    if form.get("weight"):
        fields["weight"] = num("weight")
    return fields


@bp.route("/edit/<slug>", methods=["GET", "POST"])
def edit(slug):
    path, saved = draft(slug)
    if request.method == "POST":
        fields = fields_from_form(request.form, saved)
        core.save_json(path, fields)
        if request.form.get("action") == "publish":
            errors = core.publish(slug, g.user["username"])
            if not errors:
                flash(f"{slug} is live.")
                return redirect(url_for("admin.live_view", slug=slug))
            for e in errors:
                flash(e)
        else:
            flash("Saved.")
        return redirect(url_for("admin.edit", slug=slug))
    w, h = core.image_size(saved["image"])
    return render_template("edit.html", slug=slug, t=saved, width=w, height=h, fonts=core.list_fonts(),
                           errors=core.validate(slug, saved)[1], limits=core, size_warning=size_warning(saved["image"]))


@bp.post("/edit/<slug>/image")
def replace_image(slug):
    path, saved = draft(slug)
    try:
        saved["image"] = core.store_image(request.files["image"].stream)
    except (KeyError, ValueError) as e:
        flash(str(e) if isinstance(e, ValueError) else "Choose a background image.")
    else:
        core.save_json(path, saved)
        flash("Background replaced. Check the text area still sits where you want it.")
        if warning := size_warning(saved["image"]):
            flash(warning)
    return redirect(url_for("admin.edit", slug=slug))


@bp.post("/preview/<slug>")
def preview(slug):
    _, saved = draft(slug)
    jpeg, errors = core.preview(slug, fields_from_form(request.form, saved), request.form.get("preview_name", ""))
    if errors:
        return jsonify(errors=errors), 422
    return jpeg, 200, {"Content-Type": "image/jpeg"}


@bp.get("/images/<name>")
def image(name):
    return send_from_directory(core.IMAGES, name)


@bp.get("/live/<slug>")
def live_view(slug):
    path = core.LIVE / f"{slug}.json"
    if not core.SLUG.fullmatch(slug) or not path.exists():
        abort(404)
    t = core.read_json(path)
    w, h = core.image_size(t["image"])
    src = f"{public_base()}/{slug}.jpg"
    snippet = (f'<img src="{src}?name=[[FIRSTNAME]]" alt="{html.escape(t["fallback"])}" '
               f'width="{w // 2}" height="{h // 2}" style="display:block;border:0;max-width:100%;height:auto;">')
    return render_template("live.html", slug=slug, t=t, src=src, snippet=snippet, width=w,
                           ok=slug in core.live()[0], copy_name=next_free(slug),
                           archived=bool(query("SELECT 1 FROM archived WHERE slug = ?", slug)))


@bp.post("/duplicate/<slug>")
def duplicate(slug):
    if not core.SLUG.fullmatch(slug):
        abort(404)
    source = next((d / f"{slug}.json" for d in (core.LIVE, core.DRAFTS) if (d / f"{slug}.json").exists()), None)
    new_slug = request.form.get("new_slug", "").strip().lower()
    if source is None:
        abort(404)
    if not core.SLUG.fullmatch(new_slug) or core.slug_taken(new_slug):
        flash("Pick a new, unused name: lowercase letters, digits and dashes.")
        return redirect(request.referrer or url_for("admin.index"))
    fields = core.read_json(source)
    fields["meta"] = {"created_by": g.user["username"], "copied_from": slug}
    core.save_json(core.DRAFTS / f"{new_slug}.json", fields)
    return redirect(url_for("admin.edit", slug=new_slug))


@bp.post("/delete/<slug>")
def delete(slug):
    if not core.SLUG.fullmatch(slug):
        abort(404)
    if request.form.get("which") == "live":
        if not g.user["is_admin"]:
            abort(403)
        (core.LIVE / f"{slug}.json").unlink(missing_ok=True)
        query("DELETE FROM archived WHERE slug = ?", slug)
        flash(f"{slug} is no longer live. Emails already sent with it will show a broken image.")
    else:
        (core.DRAFTS / f"{slug}.json").unlink(missing_ok=True)
        query("DELETE FROM archived WHERE slug = ?", slug)
        flash(f"Draft {slug} deleted.")
    return redirect(url_for("admin.index"))


@bp.post("/archive/<slug>")
def archive(slug):
    if not core.SLUG.fullmatch(slug) or not core.slug_taken(slug):
        abort(404)
    if request.form.get("restore"):
        query("DELETE FROM archived WHERE slug = ?", slug)
        flash(f"{slug} restored.")
    else:
        query("INSERT OR IGNORE INTO archived (slug, archived_by, archived_at) VALUES (?, ?, ?)",
              slug, g.user["username"], time.time())
        flash(f"{slug} archived." + (" Its image still works in emails already sent." if (core.LIVE / f"{slug}.json").exists() else ""))
    return redirect(request.referrer or url_for("admin.index"))


@bp.route("/fonts", methods=["GET", "POST"])
@admin_only
def fonts():
    if request.method == "POST":
        upload = request.files.get("font")
        try:
            if not upload or not upload.filename:
                raise ValueError("Choose a font file.")
            flash(f"Installed {core.store_font(upload.filename, upload.read())}.")
        except ValueError as e:
            flash(str(e))
        return redirect(url_for("admin.fonts"))
    return render_template("fonts.html", fonts=core.list_fonts())


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "add-user":
        sys.exit("usage: python admin.py add-user USERNAME [--admin]")
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Again: "):
        sys.exit("Passwords don't match.")
    error = add_user(sys.argv[2], password, "--admin" in sys.argv)
    if error:
        sys.exit(error)
    print(f"Added {sys.argv[2]}.")

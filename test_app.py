"""Run: uv run python test_app.py  (uses a throwaway DATA_DIR)"""
import io
import os
import tempfile
from pathlib import Path

DATA = Path(tempfile.mkdtemp())
os.environ.update(DATA_DIR=str(DATA), COOKIE_SECURE="0")

import admin  # noqa: E402
import core  # noqa: E402
from app import app  # noqa: E402

# --- names ---
chars = core.charset(core.font_path("LibreFranklin.ttf"))
for raw, want in [("Ben", "Ben"), ("  BEN ", "Ben"), ("mary ann", "Mary Ann"), ("McDonald", "McDonald"),
                  ("O'Brien", "O'Brien"), ("José", "José"), ("Nguyễn", "Nguyễn")]:
    assert core.clean_name(raw, chars) == want, raw
for bad in [None, "", "   ", "[[FIRSTNAME]]", "Ben <script>", "http://x.co", "a" * 31, "李", "123", "--", "Ben\n!"]:
    assert core.clean_name(bad, chars) == "", bad

# --- template rules (sample template was seeded on import) ---
good = core.read_json(core.LIVE / "orientation.json")
assert core.validate("orientation", good)[1] == []
for slug, raw in {
    "Bad Name": good, "x": {**good, "colour": "#FFFFFF"}, "x2": {k: v for k, v in good.items() if k != "fallback"},
    "x3": {**good, "message": "Register!"}, "x4": {**good, "message": "Hi {name}, {other}"},
    "x5": {**good, "message": "x" * 81}, "x6": {**good, "message": "Hi {name} ★"},
    "x7": {**good, "box": [0, 0, 600, 500]}, "x8": {**good, "box": [32, 32, 700, 468]},
    "x9": {**good, "color": "white"}, "x10": {**good, "min_size": 10}, "x11": {**good, "align": "justify"},
    "x12": {**good, "font": "missing.ttf"}, "x13": {**good, "box": [32, 32, 150, 90]},
    "x14": {**good, "image": "../users.db"}, "x15": {**good, "weight": 1000},
}.items():
    assert core.validate(slug, raw)[0] is None, slug

# --- uploads ---
def png(w, h):
    buf = io.BytesIO()
    core.Image.new("RGB", (w, h), "navy").save(buf, "PNG")
    buf.seek(0)
    return buf

assert core.image_size(core.store_image(png(2400, 1001))) == (1200, 500)  # scaled down, made even
thin = core.store_image(png(1200, 120))  # 600x60 strip banner, the thinnest allowed
thin_fields = {**core.new_draft_fields(thin), "message": "Hi {name}!", "fallback": "Hi there!"}
assert core.validate("thin", thin_fields)[1] == [], core.validate("thin", thin_fields)[1]
for bad in [png(300, 300), png(1200, 2000), png(1200, 118), io.BytesIO(b"not an image")]:
    try:
        core.store_image(bad)
        raise AssertionError("accepted bad image")
    except ValueError:
        pass

# --- web ---
assert admin.add_user("boss", "correct horse battery", is_admin=True) is None
assert admin.add_user("staff", "staff password 1") is None
assert admin.add_user("staff", "staff password 1")  # duplicate
assert admin.add_user("x", "short")

c = app.test_client()
assert c.get("/admin/").status_code == 302  # login required
assert c.get("/orientation.jpg?name=Ben").mimetype == "image/jpeg"  # public endpoint needs no login
assert c.get("/nope.jpg").status_code == 404


def login(client, user, password):
    r = client.post("/admin/login", data={"username": user, "password": password})
    with client.session_transaction() as s:
        return r, s.get("csrf")


r, csrf = login(c, "staff", "wrong")
assert csrf is None and b"Wrong" in r.data
r, csrf = login(c, "staff", "staff password 1")
assert r.status_code == 302 and csrf
assert c.get("/admin/users").status_code == 403  # staff aren't admins
assert c.post("/admin/new", data={"slug": "fall", "image": (png(1200, 500), "bg.png")}).status_code == 400  # no CSRF token

# create -> edit -> preview -> publish
r = c.post("/admin/new", data={"csrf": csrf, "slug": "fall", "image": (png(1200, 500), "bg.png")})
assert r.headers["Location"].endswith("/admin/edit/fall"), r.headers
assert c.get("/admin/edit/fall").status_code == 200
assert b"will appear" not in c.get("/admin/edit/fall").data  # full width: no warning
r = c.post("/admin/new", data={"csrf": csrf, "slug": "narrow", "image": (png(600, 200), "bg.png")}, follow_redirects=True)
assert b"will appear 300px wide" in r.data
form = {"csrf": csrf, "font": "LibreFranklin.ttf", "weight": "500", "message": "See you this fall, {name}!",
        "fallback": "See you this fall!", "x0": "40", "y0": "40", "x1": "600", "y1": "460", "align": "center",
        "valign": "middle", "color": "#ffcc00", "min_size": "24", "max_size": "90", "line_spacing": "1.1",
        "preview_name": "Zelda"}
r = c.post("/admin/preview/fall", data=form)
assert r.status_code == 200 and r.mimetype == "image/jpeg"
r = c.post("/admin/preview/fall", data={**form, "message": "no placeholder"})
assert r.status_code == 422 and "{name}" in r.json["errors"][0]
assert c.get("/fall.jpg").status_code == 404  # not live yet
r = c.post("/admin/edit/fall", data={**form, "action": "publish"})
assert r.headers["Location"].endswith("/admin/live/fall")
assert not (core.DRAFTS / "fall.json").exists()
assert c.get("/fall.jpg?name=Zelda").mimetype == "image/jpeg"  # live without a restart
assert c.get("/admin/edit/fall").status_code == 404  # live = locked
assert b"[[FIRSTNAME]]" in c.get("/admin/live/fall").data
r = c.post("/admin/duplicate/fall", data={"csrf": csrf, "new_slug": "fall-2"})
assert r.headers["Location"].endswith("/admin/edit/fall-2")
assert c.post("/admin/delete/fall", data={"csrf": csrf, "which": "live"}).status_code == 403  # admins only

# copy: suggested names, drafts can be copied too, names can't be reused
assert admin.next_free("fall") == "fall-3" and admin.next_free("fall-2") == "fall-3"
assert c.post("/admin/duplicate/fall-2", data={"csrf": csrf, "new_slug": "fall-3"}).headers["Location"].endswith("/edit/fall-3")
assert core.read_json(core.DRAFTS / "fall-3.json")["meta"]["copied_from"] == "fall-2"
assert "/edit/" not in c.post("/admin/duplicate/fall", data={"csrf": csrf, "new_slug": "fall-3"}).headers["Location"]

# archive: hidden from the lists, still served, restorable
page = lambda: c.get("/admin/").get_data(as_text=True)  # noqa: E731
assert c.post("/admin/archive/fall", data={"csrf": csrf}).status_code == 302
assert c.post("/admin/archive/fall-3", data={"csrf": csrf}).status_code == 302
live_section = page().split("<h2>Live</h2>")[1].split("Archived (")[0]
assert "/admin/live/fall\"" not in live_section and "Archived (2)" in page()
assert c.get("/fall.jpg?name=Ben").status_code == 200  # sent emails keep working
assert c.post("/admin/archive/fall", data={"csrf": csrf, "restore": "1"}).status_code == 302
assert "Archived (1)" in page() and "/admin/live/fall\"" in page().split("Archived (")[0]
assert c.post("/admin/archive/nope", data={"csrf": csrf}).status_code == 404

# no recipient names are ever written to disk
assert not any(b"Zelda" in p.read_bytes() for p in DATA.rglob("*") if p.is_file())

# lockout after repeated failures
for _ in range(admin.MAX_FAILURES):
    login(app.test_client(), "boss", "nope")
r, csrf_boss = login(app.test_client(), "boss", "correct horse battery")
assert csrf_boss is None and b"Too many" in r.data

assert c.get("/healthz").json == {"templates": ["fall", "orientation"]}  # archiving never unpublishes
print("ok")

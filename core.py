"""Template storage, validation and drawing. No web code in here.

Data layout (DATA_DIR, a Docker volume):
  images/<hash>.png   uploaded backgrounds, normalized, immutable
  fonts/              fonts uploaded by admins (bundled fonts live in ./fonts)
  drafts/<slug>.json  work in progress, editable
  live/<slug>.json    published, served at /<slug>.jpg, never edited
"""
import hashlib
import io
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
LIVE, DRAFTS, IMAGES, FONTS = (DATA / d for d in ("live", "drafts", "images", "fonts"))
BUNDLED_FONTS = ROOT / "fonts"
for d in (LIVE, DRAFTS, IMAGES, FONTS):
    d.mkdir(parents=True, exist_ok=True)

Image.MAX_IMAGE_PIXELS = 50_000_000  # reject decompression bombs early

MAX_NAME = 30  # longer names get the fallback text
MAX_MESSAGE = 80
IMAGE_WIDTH = (400, 1200)  # 2x pixels: shown at half size, so 200-600px wide in the email
IMAGE_HEIGHT = (120, 1600)  # 60-800px tall in the email; below 60 one line of text stops fitting
MIN_MARGIN = 16  # text area must stay this far from the image edge
FONT_SIZE = (20, 160)  # 2x pixels; 20 = 10px in the email
PROBE_NAME = "Christopher"  # every template must fit a name at least this long
NAME_PUNCTUATION = " '’-."
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,49}")
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
COLOR = re.compile(r"#[0-9a-fA-F]{6}")

DEFAULTS = {"weight": None, "align": "left", "valign": "middle", "line_spacing": 1.15, "meta": None}
REQUIRED = {"image", "font", "message", "fallback", "box", "color", "min_size", "max_size"}


# ---------- files ----------

def atomic_write(path, data):
    """Write via rename so readers (and other workers) never see a half-written file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def save_json(path, obj):
    atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=False).encode())


def read_json(path):
    return json.loads(path.read_text())


def font_path(name):
    return FONTS / name if (FONTS / name).is_file() else BUNDLED_FONTS / name


def list_fonts():
    """[{name, weights: (min, max) or None}] for the editor's font picker."""
    names = {p.name for d in (BUNDLED_FONTS, FONTS) for p in d.glob("*") if p.suffix.lower() in (".ttf", ".otf")}
    return [{"name": n, "weights": weight_range(font_path(n))} for n in sorted(names)]


@lru_cache(maxsize=None)
def weight_range(path):
    axes = [a for a in TTFont(path)["fvar"].axes if a.axisTag == "wght"] if "fvar" in TTFont(path) else []
    return (int(axes[0].minValue), int(axes[0].maxValue)) if axes else None


@lru_cache(maxsize=None)
def charset(path):
    return frozenset(TTFont(path).getBestCmap())


def store_font(filename, data):
    """Validate an uploaded font; returns its stored name. Raises ValueError with a message for staff."""
    stem, ext = os.path.splitext(filename)
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-") + ext.lower()
    if ext.lower() not in (".ttf", ".otf") or not FILENAME.fullmatch(name):
        raise ValueError("Fonts must be .ttf or .otf files.")
    if font_path(name).exists():
        raise ValueError(f"A font called {name} already exists.")
    try:
        TTFont(io.BytesIO(data)).getBestCmap()
        ImageFont.truetype(io.BytesIO(data), 20)
    except Exception:
        raise ValueError("That file isn't a usable font.")
    atomic_write(FONTS / name, data)
    return name


def store_image(fileobj):
    """Validate and normalize an uploaded background; returns its stored name.

    Re-encoding strips metadata (EXIF, GPS). Raises ValueError with a message for staff.
    """
    try:
        img = Image.open(fileobj)
        img.load()
    except Exception:
        raise ValueError("That file isn't a PNG or JPEG image.")
    if img.format not in ("PNG", "JPEG"):
        raise ValueError("Please upload a PNG or JPEG image.")
    img = ImageOps.exif_transpose(img).convert("RGB")
    if img.width < IMAGE_WIDTH[0]:
        raise ValueError(f"The image is {img.width}px wide. It needs to be at least {IMAGE_WIDTH[0]}px "
                         "(twice the width it will show at in the email).")
    if img.width > IMAGE_WIDTH[1]:
        img = img.resize((IMAGE_WIDTH[1], round(img.height * IMAGE_WIDTH[1] / img.width)), Image.LANCZOS)
    img = img.crop((0, 0, img.width // 2 * 2, img.height // 2 * 2))
    if not IMAGE_HEIGHT[0] <= img.height <= IMAGE_HEIGHT[1]:
        raise ValueError(f"After sizing to {img.width}px wide the image is {img.height}px tall; "
                         f"it must be {IMAGE_HEIGHT[0]}-{IMAGE_HEIGHT[1]}px.")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    data = buf.getvalue()
    name = hashlib.sha256(data).hexdigest()[:20] + ".png"
    atomic_write(IMAGES / name, data)
    return name


def image_size(name):
    with Image.open(IMAGES / name) as img:
        return img.size


# ---------- validation ----------

def validate(slug, raw):
    """Return (template, errors). Strict: anything unexpected is an error, nothing is guessed."""
    if not SLUG.fullmatch(slug):
        return None, ["Name must be lowercase letters, digits and dashes (e.g. fall-orientation), up to 50 characters."]
    errors = []
    unknown = set(raw) - REQUIRED - set(DEFAULTS)
    missing = REQUIRED - set(raw)
    if unknown:
        errors.append(f"Unknown settings: {', '.join(sorted(unknown))}")
    if missing:
        return None, errors + [f"Missing settings: {', '.join(sorted(missing))}"]
    t = {**DEFAULTS, **raw}

    for key in ("image", "font"):
        if not (isinstance(t[key], str) and FILENAME.fullmatch(t[key])):
            return None, errors + [f"Choose a{'n' if key == 'image' else ''} {key}."]
    t["image"], t["font"] = IMAGES / t["image"], font_path(t["font"])
    if not t["image"].is_file():
        return None, errors + ["The background image is missing; upload it again."]
    if not t["font"].is_file():
        return None, errors + [f"The font {t['font'].name} isn't installed."]
    try:
        with Image.open(t["image"]) as img:
            fmt, (w, h) = img.format, img.size
        chars = charset(t["font"])
        weights = weight_range(t["font"])
    except Exception:
        return None, errors + ["The image or font file can't be read."]
    t["chars"] = chars

    if fmt not in ("PNG", "JPEG"):
        errors.append(f"The image must be PNG or JPEG, not {fmt}.")
    if not (IMAGE_WIDTH[0] <= w <= IMAGE_WIDTH[1] and w % 2 == 0):
        errors.append(f"The image is {w}px wide; it must be an even number from {IMAGE_WIDTH[0]} to {IMAGE_WIDTH[1]}.")
    if not (IMAGE_HEIGHT[0] <= h <= IMAGE_HEIGHT[1] and h % 2 == 0):
        errors.append(f"The image is {h}px tall; it must be an even number from {IMAGE_HEIGHT[0]} to {IMAGE_HEIGHT[1]}.")

    for key, label in (("message", "Message"), ("fallback", "Message without a name")):
        text = t[key]
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE:
            errors.append(f"{label} must be 1-{MAX_MESSAGE} characters.")
            continue
        body = text.replace("{name}", "", 1) if key == "message" else text
        if "{" in body or "}" in body:
            errors.append(f"{label}: the only placeholder allowed is {{name}}, once, and only in the main message.")
        missing_glyphs = {c for c in body if not c.isspace() and ord(c) not in chars}
        if missing_glyphs:
            errors.append(f"{label}: the font can't draw {' '.join(sorted(missing_glyphs))}")
    if isinstance(t["message"], str) and "{name}" not in t["message"]:
        errors.append("Message must contain {name} where the first name goes.")

    box = t["box"]
    if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, int) for v in box)):
        errors.append("Draw the text area on the image.")
    else:
        x0, y0, x1, y1 = box
        if not (MIN_MARGIN <= x0 < x1 <= w - MIN_MARGIN and MIN_MARGIN <= y0 < y1 <= h - MIN_MARGIN):
            errors.append(f"The text area must be inside the image and at least {MIN_MARGIN}px from every edge.")

    if not (isinstance(t["color"], str) and COLOR.fullmatch(t["color"])):
        errors.append('Text colour must look like "#FFFFFF".')
    if t["align"] not in ("left", "center", "right"):
        errors.append("Horizontal alignment must be left, center or right.")
    if t["valign"] not in ("top", "middle", "bottom"):
        errors.append("Vertical alignment must be top, middle or bottom.")
    if not (isinstance(t["line_spacing"], (int, float)) and 1.0 <= t["line_spacing"] <= 1.6):
        errors.append("Line spacing must be 1.0-1.6.")
    sizes = (t["min_size"], t["max_size"])
    if not (all(isinstance(s, int) for s in sizes) and FONT_SIZE[0] <= sizes[0] <= sizes[1] <= FONT_SIZE[1]):
        errors.append(f"Text sizes must be whole numbers from {FONT_SIZE[0]} to {FONT_SIZE[1]}, smallest ≤ largest.")
    if t["weight"] is not None:
        if not weights:
            errors.append(f"{t['font'].name} has a single weight; leave weight empty.")
        elif not (isinstance(t["weight"], int) and weights[0] <= t["weight"] <= weights[1]):
            errors.append(f"Weight must be {weights[0]}-{weights[1]}.")
    if t["meta"] is not None and not isinstance(t["meta"], dict):
        errors.append("meta must be an object.")

    if not errors:
        if not layout(t, t["fallback"]):
            errors.append("The message without a name doesn't fit the text area, even at the smallest size.")
        if not layout(t, t["message"].format(name=PROBE_NAME)):
            errors.append(f'The message doesn\'t fit with a name like "{PROBE_NAME}". '
                          "Shorten it, enlarge the text area, or lower the smallest size.")
    return (None if errors else t), errors


def clean_name(raw, chars):
    """Return a display-safe first name, or '' to use the fallback text.

    Anyone can edit the URL, so allow only letters the font can draw plus space ' ’ - .
    """
    name = re.sub(r"\s+", " ", (raw or "").strip())
    if not name or len(name) > MAX_NAME or not any(ch.isalpha() for ch in name):
        return ""
    if not all((ch.isalpha() or ch in NAME_PUNCTUATION) and (ch == " " or ord(ch) in chars) for ch in name):
        return ""
    if name.isupper() or name.islower():  # "BEN"/"ben" -> "Ben", but keep "McDonald"
        name = name.title()
    return name


# ---------- drawing ----------

@lru_cache(maxsize=256)
def font(path, size, weight):
    f = ImageFont.truetype(str(path), size)
    if weight is not None:
        axes = TTFont(path)["fvar"].axes  # same order Pillow uses
        f.set_variation_by_axes([weight if a.axisTag == "wght" else a.defaultValue for a in axes])
    return f


def wrap(text, f, width):
    """Greedy word wrap, also breaking after hyphens; None if a piece is wider than the area."""
    lines = []
    for word in re.findall(r"[^\s-]*-|\S+", text):
        if f.getlength(word) > width:
            return None
        joined = lines[-1] + ("" if lines and lines[-1].endswith("-") else " ") + word if lines else word
        if lines and f.getlength(joined) <= width:
            lines[-1] = joined
        else:
            lines.append(word)
    return lines


def layout(t, text):
    """Largest font size whose wrapped text fits the area, or None."""
    x0, y0, x1, y1 = t["box"]
    for size in range(t["max_size"], t["min_size"] - 1, -2):
        f = font(t["font"], size, t["weight"])
        lines = wrap(text, f, x1 - x0)
        line_h = round(size * t["line_spacing"])
        if lines and line_h * len(lines) <= y1 - y0:
            return f, lines, line_h
    return None


def draw(t, name):
    """JPEG bytes of template t with a cleaned name ('' = fallback text)."""
    fit = name and layout(t, t["message"].format(name=name))
    f, lines, line_h = fit or layout(t, t["fallback"])
    img = Image.open(t["image"]).convert("RGB")
    canvas = ImageDraw.Draw(img)
    x0, y0, x1, y1 = t["box"]
    height = line_h * len(lines)
    y = {"top": y0, "middle": y0 + (y1 - y0 - height) // 2, "bottom": y1 - height}[t["valign"]]
    for line in lines:
        slack = x1 - x0 - f.getlength(line)
        x = x0 + {"left": 0, "center": slack / 2, "right": slack}[t["align"]]
        canvas.text((x, y), line, font=f, fill=t["color"])
        y += line_h
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88, optimize=True, progressive=True)
    return buf.getvalue()


def preview(slug, raw, name):
    """(jpeg, errors) for unsaved editor settings."""
    t, errors = validate(slug, raw)
    return (draw(t, clean_name(name, t["chars"])), []) if t else (None, errors)


# ---------- live templates ----------

_live = {"stamp": None, "templates": {}, "errors": {}}


def load_dir(directory):
    templates, errors = {}, {}
    for path in sorted(directory.glob("*.json")):
        try:
            raw = read_json(path)
            t, errs = validate(path.stem, raw) if isinstance(raw, dict) else (None, ["must be a JSON object"])
        except (OSError, json.JSONDecodeError) as e:
            t, errs = None, [f"unreadable: {e}"]
        if t:
            t["cache"] = {}
            templates[path.stem] = t
        else:
            errors[path.name] = errs
    return templates, errors


def live():
    """(templates, errors) for live/, reloaded whenever a publish or delete changes the directory.

    Every write goes through a rename, which bumps the directory mtime, so each gunicorn
    worker notices on its next request. No restart needed.
    """
    stamp = LIVE.stat().st_mtime_ns
    if stamp != _live["stamp"]:
        templates, errors = load_dir(LIVE)
        _live.update(stamp=stamp, templates=templates, errors=errors)
    return _live["templates"], _live["errors"]


def render_live(slug, raw_name):
    """JPEG for a live template, or None if there's no such template. The name is only held in memory."""
    t = live()[0].get(slug)
    if t is None:
        return None
    name = clean_name(raw_name, t["chars"])
    cache = t["cache"]
    if name not in cache:
        if len(cache) >= 2000:
            cache.clear()  # ponytail: crude bound per template per worker; the CDN does the real caching
        cache[name] = draw(t, name)
    return cache[name]


def publish(slug, username):
    """Validate a draft and move it to live/. Returns a list of errors (empty on success)."""
    path, target = DRAFTS / f"{slug}.json", LIVE / f"{slug}.json"
    if target.exists():
        return ["A live template already has this name."]
    raw = read_json(path)
    _, errors = validate(slug, raw)
    if errors:
        return errors
    raw["meta"] = {**(raw.get("meta") or {}), "published_by": username,
                   "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    save_json(target, raw)
    path.unlink()
    return []


def slug_taken(slug):
    return (DRAFTS / f"{slug}.json").exists() or (LIVE / f"{slug}.json").exists()


def new_draft_fields(image):
    """Starting settings for a fresh background: text on the left 40%, white, padded."""
    w, h = image_size(image)
    pad, vpad = max(MIN_MARGIN, w // 20), max(MIN_MARGIN, min(w // 20, h // 6))  # thin banners get less vertical padding
    return {"image": image, "font": "LibreFranklin.ttf", "weight": 500, "message": "", "fallback": "",
            "box": [pad, vpad, round(w * 0.4) - pad // 2, h - vpad], "align": "left", "valign": "middle",
            "color": "#FFFFFF", "min_size": FONT_SIZE[0] + 2, "max_size": round(w * 0.09), "line_spacing": 1.15}


def seed_sample():
    """First run only: install the sample orientation template so there's a working example."""
    if any(LIVE.glob("*.json")) or any(DRAFTS.glob("*.json")):
        return
    raw = read_json(ROOT / "sample" / "orientation.json")
    with open(ROOT / "sample" / raw["image"], "rb") as f:
        raw["image"] = store_image(f)
    raw["meta"] = {"published_by": "sample"}
    save_json(LIVE / "orientation.json", raw)

"""Photo Boss client gallery starter API (issue #61).

Run: PHOTO_BOSS_GALLERY_OWNER_TOKEN=... python -m client_gallery.server
Requires aiohttp and Pillow, already in project requirements.
Use behind HTTPS reverse proxy. Do not expose this preview service as production.
"""
import asyncio
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
from pathlib import Path
from PIL import Image, ImageEnhance, ImageOps
from aiohttp import web

ROOT = Path(os.getenv("PHOTO_BOSS_GALLERY_DATA", "gallery_data"))
DB = ROOT / "gallery.sqlite3"
LABELS = {
    "download": ("Скачать", True, 0),
    "favorite": ("Избранное", True, 0),
    "share": ("Поделиться", True, 0),
    "editor": ("Обработать фото", True, 29),
    "album_ai": ("Альбом с ИИ", True, 99),
    "album_manual": ("Собрать альбом", True, 99),
    "song": ("Песня о семье", False, 0),
    "video": ("Семейное видео", False, 0),
}
MAX_BYTES = 15 * 1024 * 1024

def connect():
    ROOT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS galleries (
       id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, title TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS photos (
       id TEXT PRIMARY KEY, gallery_id TEXT NOT NULL, filename TEXT NOT NULL,
       FOREIGN KEY(gallery_id) REFERENCES galleries(id)
    );
    CREATE TABLE IF NOT EXISTS button_settings (
       key TEXT PRIMARY KEY, label TEXT NOT NULL, enabled INTEGER NOT NULL, price INTEGER NOT NULL
    );
    """)
    for key, (label, enabled, price) in LABELS.items():
        con.execute("INSERT OR IGNORE INTO button_settings VALUES (?,?,?,?)",
                    (key, label, int(enabled), price))
    con.commit()
    return con

def owner(request):
    configured = os.getenv("PHOTO_BOSS_GALLERY_OWNER_TOKEN")
    incoming = request.headers.get("X-Owner-Token", "")
    if not configured or not hmac.compare_digest(incoming, configured):
        raise web.HTTPForbidden(text="Owner authorization required")

def gallery_access(request, con, gallery_id):
    token = request.headers.get("X-Gallery-Token") or request.query.get("token", "")
    row = con.execute("SELECT * FROM galleries WHERE id=?", (gallery_id,)).fetchone()
    if not row or not token or not hmac.compare_digest(
        row["token_hash"], hashlib.sha256(token.encode()).hexdigest()
    ):
        raise web.HTTPNotFound()
    return row

def allowed(con, key):
    row = con.execute("SELECT * FROM button_settings WHERE key=?", (key,)).fetchone()
    return row and row["enabled"]

async def make_gallery(request):
    owner(request)
    body = await request.json()
    title = str(body.get("title", "Фотосессия")).strip()[:120]
    gid, token = secrets.token_urlsafe(12), secrets.token_urlsafe(32)
    with connect() as con:
        con.execute("INSERT INTO galleries VALUES (?,?,?)",
                    (gid, hashlib.sha256(token.encode()).hexdigest(), title))
    return web.json_response({"id": gid, "token": token, "title": title}, status=201)

async def settings(request):
    owner(request)
    with connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM button_settings")]
    return web.json_response(rows)

async def change_settings(request):
    owner(request)
    key = request.match_info["key"]
    if key not in LABELS:
        raise web.HTTPNotFound()
    data = await request.json()
    with connect() as con:
        current = con.execute("SELECT * FROM button_settings WHERE key=?", (key,)).fetchone()
        label = str(data.get("label", current["label"])).strip()[:55]
        enabled = data.get("enabled", bool(current["enabled"]))
        price = data.get("price", current["price"])
        if not label or not isinstance(enabled, bool) or type(price) is not int or not (0 <= price <= 100000):
            raise web.HTTPBadRequest(text="Invalid label, enabled or price")
        con.execute("UPDATE button_settings SET label=?, enabled=?, price=? WHERE key=?",
                    (label, int(enabled), price, key))
    return web.json_response({"key": key, "label": label, "enabled": enabled, "price": price})

async def gallery(request):
    gid = request.match_info["gallery_id"]
    with connect() as con:
        row = gallery_access(request, con, gid)
        photos = [{"id": p["id"]} for p in con.execute(
            "SELECT id FROM photos WHERE gallery_id=?", (gid,))]
        buttons = [dict(r) for r in con.execute(
            "SELECT * FROM button_settings WHERE enabled=1")]
    return web.json_response({"title": row["title"], "photos": photos, "buttons": buttons,
                              "payment_ready": False})

async def upload(request):
    owner(request)
    gid = request.match_info["gallery_id"]
    with connect() as con:
        if not con.execute("SELECT id FROM galleries WHERE id=?", (gid,)).fetchone():
            raise web.HTTPNotFound()
    data = await request.read()
    if len(data) > MAX_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_BYTES, actual_size=len(data))
    try:
        im = Image.open(io.BytesIO(data))
        im.verify()
        im = Image.open(io.BytesIO(data))
        if im.width * im.height > 45_000_000:
            raise ValueError("Image too large")
        im = ImageOps.exif_transpose(im).convert("RGB")
    except Exception:
        raise web.HTTPBadRequest(text="A valid photo is required")
    pid = secrets.token_urlsafe(12)
    folder = ROOT / "photos" / gid
    folder.mkdir(parents=True, exist_ok=True)
    filename = pid + ".jpg"
    im.save(folder / filename, quality=91)
    with connect() as con:
        con.execute("INSERT INTO photos VALUES (?,?,?)", (pid, gid, filename))
    return web.json_response({"id": pid}, status=201)

def photo_path(con, gid, pid):
    p = con.execute("SELECT filename FROM photos WHERE gallery_id=? AND id=?",
                    (gid, pid)).fetchone()
    if not p:
        raise web.HTTPNotFound()
    return ROOT / "photos" / gid / p["filename"]

async def preview(request):
    gid, pid = request.match_info["gallery_id"], request.match_info["photo_id"]
    with connect() as con:
        gallery_access(request, con, gid)
        path = photo_path(con, gid, pid)
        edit_enabled = bool(allowed(con, "editor"))
    exposure = request.query.get("brightness", "1")
    try:
        brightness = float(exposure)
        if not (0.6 <= brightness <= 1.4):
            raise ValueError()
    except ValueError:
        raise web.HTTPBadRequest(text="Brightness must be between 0.6 and 1.4")
    if brightness != 1.0 and not edit_enabled:
        raise web.HTTPForbidden(text="Editor disabled")
    with Image.open(path) as source:
        image = source.copy()
    image.thumbnail((1024, 1024))
    image = ImageEnhance.Brightness(image).enhance(brightness)
    # Preview only: no paid full-resolution export without verified payment.
    stream = io.BytesIO()
    image.save(stream, format="JPEG", quality=80)
    return web.Response(body=stream.getvalue(), content_type="image/jpeg",
                        headers={"Cache-Control": "private, no-store"})

async def checkout(request):
    gid = request.match_info["gallery_id"]
    with connect() as con:
        gallery_access(request, con, gid)
    raise web.HTTPNotImplemented(text="Payment provider not connected; no charges taken")

def create_app():
    connect().close()
    app = web.Application(client_max_size=MAX_BYTES)
    app.add_routes([
        web.post("/api/galleries", make_gallery),
        web.get("/api/owner/buttons", settings),
        web.patch("/api/owner/buttons/{key}", change_settings),
        web.get("/api/galleries/{gallery_id}", gallery),
        web.post("/api/galleries/{gallery_id}/photos", upload),
        web.get("/api/galleries/{gallery_id}/photos/{photo_id}/preview", preview),
        web.post("/api/galleries/{gallery_id}/checkout", checkout),
    ])
    return app

if __name__ == "__main__":
    web.run_app(create_app(), host="127.0.0.1", port=int(os.getenv("PORT", "8099")))

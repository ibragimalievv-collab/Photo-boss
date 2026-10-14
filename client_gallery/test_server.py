"""Integration smoke tests: python -m unittest discover -s client_gallery -p 'test_*.py'"""
import io
import os
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import AioHTTPTestCase
from PIL import Image
from client_gallery import server


class GalleryTests(AioHTTPTestCase):
    async def get_application(self):
        self.temp = tempfile.TemporaryDirectory()
        server.ROOT = Path(self.temp.name)
        server.DB = server.ROOT / "gallery.sqlite3"
        os.environ["PHOTO_BOSS_GALLERY_OWNER_TOKEN"] = "owner-test-token"
        return server.create_app()

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.temp.cleanup()
        os.environ.pop("PHOTO_BOSS_GALLERY_OWNER_TOKEN", None)

    async def test_protected_gallery_and_button_settings(self):
        r = await self.client.post("/api/galleries", json={"title": "Семья"})
        self.assertEqual(r.status, 403)
        await r.release()
        r = await self.client.post("/api/galleries", json={"title": "Семья"},
                                   headers={"X-Owner-Token": "owner-test-token"})
        self.assertEqual(r.status, 201)
        created = await r.json()
        gid, token = created["id"], created["token"]
        r = await self.client.get(f"/api/galleries/{gid}")
        self.assertEqual(r.status, 404)
        await r.release()
        r = await self.client.get(f"/api/galleries/{gid}", headers={"X-Gallery-Token": token})
        self.assertEqual(r.status, 200)
        self.assertFalse((await r.json())["payment_ready"])
        r = await self.client.patch("/api/owner/buttons/editor",
                                    json={"label": "Ретушь", "enabled": False, "price": 39},
                                    headers={"X-Owner-Token": "owner-test-token"})
        self.assertEqual(r.status, 200)
        r = await self.client.get(f"/api/galleries/{gid}", headers={"X-Gallery-Token": token})
        buttons = (await r.json())["buttons"]
        self.assertNotIn("editor", [b["key"] for b in buttons])
        r = await self.client.post(f"/api/galleries/{gid}/checkout",
                                   headers={"X-Gallery-Token": token})
        self.assertEqual(r.status, 501)
        await r.release()

    async def test_photo_preview(self):
        r = await self.client.post("/api/galleries", json={"title": "Test"},
                                   headers={"X-Owner-Token": "owner-test-token"})
        info = await r.json()
        im = Image.new("RGB", (60, 40), "blue")
        output = io.BytesIO()
        im.save(output, format="JPEG")
        r = await self.client.post(f"/api/galleries/{info['id']}/photos",
                                   data=output.getvalue(),
                                   headers={"X-Owner-Token": "owner-test-token"})
        self.assertEqual(r.status, 201)
        pid = (await r.json())["id"]
        r = await self.client.get(f"/api/galleries/{info['id']}/photos/{pid}/preview",
                                  headers={"X-Gallery-Token": info["token"]})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.content_type, "image/jpeg")
        await r.read()


if __name__ == "__main__":
    unittest.main()

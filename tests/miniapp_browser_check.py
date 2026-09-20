"""Browser UI checks only. API authentication is tested separately in PostgreSQL.

All HTTP requests are intercepted: this script cannot contact production.
"""
import json
import mimetypes
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WEBAPP = ROOT / "app" / "webapp"
OUT = ROOT / "qa-release"
OUT.mkdir(exist_ok=True)
DAY = str(date(2026, 9, 20))
IMAGES = {
    "assets/studio.jpg": "woman/04.jpg", "assets/academy/hero.jpg": "family-lifestyle.jpg",
    "assets/academy/family.jpg": "family-portrait.jpg", "assets/academy/child.jpg": "child.jpg",
    "assets/academy/couple.jpg": "couple.jpg", "assets/academy/coast.jpg": "family/03.jpg",
    "assets/academy/evening.jpg": "couple/04.jpg", "assets/academy/lens.jpg": "man.jpg",
}


def run():
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for role in ("OWNER", "ADMIN", "PHOTOGRAPHER", "MANAGER"):
            for width in (320, 390, 768, 1440):
                context = browser.new_context(viewport={"width": width, "height": 950}, device_scale_factor=1)
                errors, unexpected = [], []
                theme = ["premium" if role == "OWNER" else "light"]
                completed = []
                perms = {"financeScope": "all" if role == "OWNER" else "today" if role == "ADMIN" else "self",
                         "audit": role == "OWNER", "manageSchedule": role in {"OWNER", "ADMIN"},
                         "manageBookings": role in {"OWNER", "ADMIN", "MANAGER"}}
                user = {"id": 3, "telegramId": 1003, "name": "Александр Тестовый", "roles": [role]}
                booking = {"id": 1, "client": "Тестовая семья", "date": DAY, "time": "14:00", "room": "101",
                           "status": "ASSIGNED", "photographerId": 3, "photographer": user["name"], "managerId": 4,
                           "hotelId": 1, "hotel": "Тестовый отель", "type": "Семейная съёмка", "frames": 0}
                shift = {"id": 1, "userId": 3, "name": user["name"], "role": "PHOTOGRAPHER", "hotelId": 1,
                         "hotel": "Тестовый отель", "date": DAY, "start": "09:00", "end": "19:00",
                         "status": "PLANNED", "attendance": "STARTED"}

                def finance(period="today"):
                    return {"period": period, "from": DAY, "to": DAY, "scope": "self" if perms["financeScope"] == "self" else "company",
                            "sales": 2100000, "cashReceived": None if perms["financeScope"] == "self" else 2100000,
                            "payroll": 415000, "employees": [{"id": 3, "name": user["name"], "role": role,
                            "sales": 2100000, "commission": 315000, "adjustments": 100000, "earned": 415000}],
                            "salesRows": [], "salesCount": 0, "salesListLimited": False, "note": "Тестовая ведомость, не рабочие данные."}

                def handler(route):
                    url = urlsplit(route.request.url)
                    if url.hostname == "telegram.org":
                        route.fulfill(content_type="application/javascript", body="window.Telegram={WebApp:{initData:'browser_fixture_not_credentials',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},isVersionAtLeast(){return false;},BackButton:{hide(){},show(){},onClick(){}},onEvent(){},HapticFeedback:{selectionChanged(){}}}};")
                        return
                    if url.hostname != "photo-boss.test":
                        unexpected.append(route.request.url)
                        route.abort()
                        return
                    if url.path.startswith("/api/miniapp/"):
                        path = url.path.removeprefix("/api/miniapp")
                        payload = json.loads(route.request.post_data or "{}")
                        query = parse_qs(url.query)
                        data, status = {}, 200
                        if path == "/me":
                            data = {"user": {**user, "theme": theme[0]}, "permissions": perms, "today": DAY, "timezone": "Europe/Moscow", "mode": "live"}
                        elif path == "/session":
                            data = {"ok": True}
                        elif path == "/preferences":
                            theme[0] = payload["theme"]
                            data = {"theme": theme[0]}
                        elif path == "/dashboard":
                            data = {"today": DAY, "team": [shift], "bookings": [booking], "finance": finance()}
                        elif path == "/bookings":
                            data = {"items": [booking] if query.get("day", [DAY])[0] == DAY else []}
                        elif path == "/finance":
                            data = finance(query.get("period", ["today"])[0])
                        elif path == "/schedule":
                            data = {"items": [shift], "employees": [{"id": 3, "name": user["name"]}], "hotels": [{"id": 1, "name": "Тестовый отель"}]}
                        elif path == "/academy":
                            data = {"lessons": [{"slug": "light", "title": "Свет в кадре", "body": "Сначала найдите мягкий свет."}], "completed": completed[:], "practices": [], "reviews": []}
                        elif path == "/academy/lessons/light":
                            if "light" not in completed:
                                completed.append("light")
                            data = {"ok": True}
                        elif path == "/audit":
                            if not perms["audit"]:
                                status, data = 403, {"error": "Только владелец"}
                            else:
                                data = {"items": [{"id": 1, "actor": user["name"], "at": DAY + "T10:00:00Z", "action": "miniapp_opened", "title": "Открыл приложение", "entity": "user", "entityId": 3, "details": "Подтверждён тестовый вход."}], "next": None}
                        else:
                            unexpected.append(url.path)
                            status, data = 404, {"error": "Unknown fixture"}
                        route.fulfill(status=status, content_type="application/json", body=json.dumps(data, ensure_ascii=False))
                        return
                    path = url.path.removeprefix("/app/") or "index.html"
                    if path in IMAGES:
                        file = ROOT / "app" / "assets" / "training" / IMAGES[path]
                    else:
                        file = WEBAPP / path
                    if not file.is_file() or ".." in Path(path).parts:
                        unexpected.append(path)
                        route.fulfill(status=404)
                        return
                    mime = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
                    route.fulfill(content_type=mime, body=file.read_bytes())

                context.route("**/*", handler)
                page = context.new_page()
                page.on("pageerror", lambda err: errors.append(str(err)))
                page.goto("https://photo-boss.test/app/")
                page.locator(".cash-card").wait_for()
                assert page.locator(".release-banner").is_visible()
                assert page.locator("#previewBar").is_hidden()
                for selected in ("premium", "light", "photo"):
                    page.locator('[data-action="themes"]').click()
                    page.locator(f'#sheet [data-theme-choice="{selected}"]').click()
                    page.wait_for_function("theme => document.documentElement.dataset.theme === theme", arg=selected)
                    page.locator("#closeSheet").click()
                    if role == "OWNER" and width == 390:
                        page.screenshot(path=str(OUT / f"home-{selected}.png"), full_page=True)
                for route_name in ("shootings", "schedule", "finance", "profile", "more", "academy"):
                    page.evaluate("r => { location.hash = r; }", route_name)
                    if route_name == "academy":
                        page.locator(".academy-cover").wait_for()
                    elif route_name == "finance":
                        page.locator(".segmented").wait_for()
                        assert page.locator('[data-period="month"]').count() == (0 if role == "ADMIN" else 1)
                    else:
                        page.locator(".page-head").wait_for()
                    page.wait_for_timeout(50)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), (role, width, route_name)
                    results.append({"role": role, "width": width, "screen": route_name})
                for tab in ("courses", "practice", "reviews", "errors", "progress", "library", "profile", "home"):
                    page.locator(f'[data-academy-tab="{tab}"]').first.click()
                    page.wait_for_timeout(50)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), (role, width, tab)
                    results.append({"role": role, "width": width, "academy": tab})
                    if role == "OWNER" and width == 390 and tab in {"home", "practice", "profile"}:
                        page.screenshot(path=str(OUT / f"academy-{tab}.png"), full_page=True)
                page.locator('[data-academy-tab="courses"]').first.click()
                page.locator('[data-lesson="light"]').click()
                page.locator('[data-complete-lesson="light"]').click()
                page.locator(".academy-tabs").wait_for()
                page.wait_for_function("document.querySelector('.academy-course.completed') !== null")
                assert completed == ["light"]
                if role != "OWNER":
                    page.evaluate("location.hash = 'audit'")
                    page.locator(".cash-card").wait_for()
                    assert page.locator(".audit-row").count() == 0
                assert not errors, errors
                assert not unexpected, unexpected
                context.close()
        browser.close()
    (OUT / "browser-report.json").write_text(json.dumps({"checks": len(results), "results": results}, ensure_ascii=False, indent=2))
    print(f"Browser route checks: {len(results)}; theme, lesson persistence and role assertions passed")


if __name__ == "__main__":
    run()

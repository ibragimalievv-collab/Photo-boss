"""Browser fixtures for work chat; no real Telegram, staff or messages."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "qa-work-chat"
OUT.mkdir(exist_ok=True)
BASE = "https://photo-boss-chat.invalid"


def handler(page_state, role, sent):
    def route(r):
        url = r.request.url
        path = url.removeprefix(BASE).split("?", 1)[0]
        if path == "/":
            return r.fulfill(
                content_type="text/html",
                body="""<!doctype html><html lang="ru" data-theme="light"><head>
                <meta name="viewport" content="width=device-width,initial-scale=1">
                <link rel="stylesheet" href="/app/css/styles.css"></head><body>
                <header id="topbar" class="topbar"><b>Photo Boss</b></header>
                <script>window.Telegram={WebApp:{initData:"fixture-only"}}</script>
                <script type="module" src="/work-chat/chat.js"></script></body></html>""",
            )
        files = {
            "/work-chat/chat.js": ROOT / "app/work_chat_ui/chat.js",
            "/work-chat/chat.css": ROOT / "app/work_chat_ui/chat.css",
            "/work-chat/calls.js": ROOT / "app/work_chat_ui/calls.js",
            "/work-chat/calls.css": ROOT / "app/work_chat_ui/calls.css",
            "/app/js/api.js": ROOT / "app/webapp/js/api.js",
            "/app/js/domain.js": ROOT / "app/webapp/js/domain.js",
            "/app/css/styles.css": ROOT / "app/webapp/css/styles.css",
        }
        if path in files:
            return r.fulfill(
                body=files[path].read_text(),
                content_type="text/css" if path.endswith(".css") else "text/javascript",
            )
        if path == "/api/miniapp/chat/calls":
            return r.fulfill(json={"calls": [], "maxParticipants": 6})
        if path == "/api/miniapp/me":
            return r.fulfill(json={"user": {"id": 1, "name": "Test", "roles": [role]}})
        if path == "/api/miniapp/chat/rules/accept":
            page_state["accepted"] = True
            return r.fulfill(json={"ok": True})
        if path == "/api/miniapp/chat/rules":
            return r.fulfill(json={
                "version": "fixture-v1", "sha256": "a" * 64,
                "text": "Общие правила\nВладелец имеет доступ к рабочим чатам.",
                "accepted": page_state["accepted"], "retentionDays": 365,
            })
        if path == "/api/miniapp/chat/unread":
            return r.fulfill(json={
                "total": 3, "general": 1, "people": {"2": 2},
            })
        if path == "/api/miniapp/chat/people":
            return r.fulfill(json={
                "general": {"id": "general", "name": "Общий чат", "unread": 1},
                "people": [{"id": 2, "name": "<script>bad()</script>",
                            "roles": ["PHOTOGRAPHER"], "unread": 2}],
                "ownerControl": role == "OWNER",
                "totalUnread": 3,
            })
        if path == "/api/miniapp/chat/messages":
            if r.request.method == "POST":
                body = json.loads(r.request.post_data)
                sent.append(body)
                return r.fulfill(status=201, json={"message": {
                    "id": 2, "senderId": 1, "recipientId": body["peerId"],
                    "body": body["body"], "createdAt": "2026-09-20T11:00:00",
                    "senderName": "Test", "attachment": None,
                }})
            return r.fulfill(json={"messages": [{
                "id": 1, "senderId": 2, "recipientId": None,
                "body": "<img src=x onerror=alert(1)>", "createdAt": "2026-09-20T10:59:00",
                "senderName": "Employee", "attachment": None,
            }], "unread": {"total": 2, "general": 0, "people": {"2": 2}}})
        if path == "/api/miniapp/chat/attachments" and r.request.method == "POST":
            return r.fulfill(status=201, json={"message": {
                "id": 9, "senderId": 1, "recipientId": None, "body": "",
                "createdAt": "2026-09-20T11:01:00", "senderName": "Test",
                "attachment": {"id": 7, "name": "report.pdf",
                    "mimeType": "application/pdf", "size": 1234, "isImage": False},
            }})
        if path == "/api/miniapp/chat/owner/threads":
            return r.fulfill(json={"threads": [{
                "a": {"id": 2, "name": "Employee A"}, "b": {"id": 3, "name": "Employee B"},
                "last": "Рабочий вопрос", "createdAt": "2026-09-20T11:00:00",
            }]})
        if path == "/api/miniapp/chat/owner/messages":
            return r.fulfill(json={"participants": [], "readOnly": True, "messages": [{
                "id": 3, "senderId": 2, "recipientId": 3, "body": "Рабочий вопрос",
                "createdAt": "2026-09-20T11:00:00", "senderName": "Employee A",
            }]})
        raise AssertionError("Unexpected request " + url)
    return route


def main():
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for role in ("OWNER", "ADMIN", "PHOTOGRAPHER", "MANAGER"):
            for width in (320, 390):
                state, sent, errors = {"accepted": False}, [], []
                page = browser.new_page(viewport={"width": width, "height": 844})
                page.on("pageerror", lambda event, bucket=errors: bucket.append(str(event)))
                page.route("**/*", handler(state, role, sent))
                page.goto(BASE)
                page.locator("[data-open-chat]").wait_for()
                page.locator(".pb-chat-trigger .pb-chat-unread").wait_for()
                assert page.locator(".pb-chat-trigger .pb-chat-unread").inner_text() == "3"
                page.locator("[data-open-chat]").wait_for()
                page.wait_for_timeout(100)
                assert "3" in page.locator("[data-open-chat]").inner_text()
                page.locator("[data-open-chat]").click()
                page.get_by_text("Принять общие правила и открыть чат", exact=True).click()
                page.locator('[data-chat="general"] .pb-chat-unread').wait_for()
                assert page.locator('[data-peer="2"] .pb-chat-unread').inner_text() == "2"
                page.get_by_text("Общий чат", exact=True).first.wait_for()
                assert page.locator(".pb-chat-unread").count() >= 2
                page.get_by_text("Общий чат", exact=True).first.click()
                page.locator("#pbChatMessages").wait_for()
                assert page.locator("#pbChatMessages img").count() == 0
                page.locator('textarea[name="body"]').fill("Тест рабочего чата")
                page.locator(".pb-chat-send").click()
                page.wait_for_function("!document.querySelector('.pb-chat-send').disabled")
                assert sent and sent[-1]["body"] == "Тест рабочего чата"
                page.locator('input[name="file"]').set_input_files({
                    "name": "report.pdf",
                    "mimeType": "application/pdf",
                    "buffer": b"%PDF-1.7 fixture",
                })
                page.get_by_text("report.pdf", exact=False).wait_for()
                page.locator(".pb-chat-send").click()
                page.wait_for_function("!document.querySelector('.pb-chat-send').disabled")
                visible_error = page.locator(".pb-chat-error:not([hidden])")
                assert visible_error.count() == 0, visible_error.first.inner_text() if visible_error.count() else ""
                assert not errors, errors
                page.locator(".pb-chat-file").wait_for(timeout=5000)
                assert "report.pdf" in page.locator(".pb-chat-file").inner_text()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2")
                page.locator('[data-chat="home"]').click()
                page.locator('[data-peer="2"]').click()
                page.locator("#pbChatMessages").wait_for()
                if role == "OWNER":
                    page.locator('[data-chat="home"]').click()
                    page.get_by_text("Контроль диалогов сотрудников", exact=True).click()
                    page.get_by_text("Employee A ↔ Employee B", exact=True).click()
                    page.get_by_text("Просмотр рабочего диалога", exact=True).wait_for()
                    assert page.locator("#pbChatCompose").count() == 0
                page.screenshot(path=str(OUT / f"{role}-{width}.png"))
                assert not errors, errors
                page.locator('[data-chat="close"]').click()
                results.append({"role": role, "width": width, "ok": True})
                page.close()
        browser.close()
    (OUT / "results.json").write_text(json.dumps(results, indent=2))
    print(f"{len(results)} work-chat fixture scenarios passed.")


if __name__ == "__main__":
    main()

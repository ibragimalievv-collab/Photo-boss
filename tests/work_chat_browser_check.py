"""Browser fixtures for work chat; no real Telegram, staff or messages."""
import base64
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

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
            "/work-chat/ringtone.js": ROOT / "app/work_chat_ui/ringtone.js",
            "/work-chat/ui.js": ROOT / "app/work_chat_ui/ui.js",
            "/work-chat/camera.js": ROOT / "app/work_chat_ui/camera.js",
            "/work-chat/recorder.js": ROOT / "app/work_chat_ui/recorder.js",
            "/app/js/api.js": ROOT / "app/webapp/js/api.js",
            "/app/js/localstore.js": ROOT / "app/webapp/js/localstore.js",
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
        if path == "/api/miniapp/chat/presence":
            if r.request.method == "POST":
                page_state["heartbeat"] = json.loads(r.request.post_data)
                return r.fulfill(json={"ok": True, "ttlSeconds": 45})
            return r.fulfill(json={"online": [2] if page_state["peerOnline"] else [], "ttlSeconds": 45})
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
                            "roles": ["PHOTOGRAPHER"], "unread": 2, "online": page_state["peerOnline"]}],
                "ownerControl": role == "OWNER",
                "totalUnread": 3,
            })
        if path.startswith("/api/miniapp/chat/messages/") and r.request.method == "DELETE":
            if role != "OWNER":
                return r.fulfill(status=403, json={"error": "Forbidden"})
            mid = int(path.rsplit('/', 1)[-1])
            page_state.setdefault('deleted', []).append(mid)
            return r.fulfill(json={"ok": True, "deletedId": mid})
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
            }], "deletedIds": page_state.get("deleted", []), "deletionCursor": len(page_state.get("deleted", [])), "unread": {"total": 2, "general": 0, "people": {"2": 2}}})
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
            for width in ((320, 390, 1200) if role == "OWNER" else (320, 390)):
                state, sent, errors = {"accepted": False, "peerOnline": True}, [], []
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
                expect(page.locator('[data-peer="2"] .pb-chat-presence')).to_have_text("В сети")
                search = page.locator('[data-chat-search]')
                search.fill('несуществующий диалог')
                expect(page.locator('[data-search-empty]')).to_be_visible()
                expect(page.locator('[data-peer="2"]')).to_be_hidden()
                search.fill('bad')
                expect(page.locator('[data-peer="2"]')).to_be_visible()
                expect(page.locator('[data-chat="general"]')).to_be_hidden()
                search.fill('')
                if role == "ADMIN":
                    page.evaluate("document.documentElement.dataset.theme='photo'")
                if role == "MANAGER":
                    page.evaluate("document.documentElement.dataset.theme='premium'")
                page.screenshot(path=str(OUT / f"list-{role}-{width}.png"))
                if role == "OWNER" and width == 390:
                    print("QA_PREVIEW list.jpg " + base64.b64encode(page.screenshot(type="jpeg", quality=60)).decode())
                page.get_by_text("Общий чат", exact=True).first.wait_for()
                assert page.locator(".pb-chat-unread").count() >= 2
                page.get_by_text("Общий чат", exact=True).first.click()
                page.locator("#pbChatMessages").wait_for()
                assert page.locator("#pbChatMessages img").count() == 0
                expect(page.locator('.pb-chat-send')).to_be_hidden()
                expect(page.locator('[data-record="audio"]')).to_be_visible()
                field = page.locator('textarea[name="body"]')
                field.fill("Черновик")
                page.locator('[data-chat="home"]').click()
                page.locator('[data-chat="general"]').click()
                expect(field).to_have_value("Черновик")
                field.fill("Тест рабочего чата")
                expect(page.locator('[data-record="audio"]')).to_be_hidden()
                field.press('Shift+Enter')
                field.type('Вторая строка')
                page.locator(".pb-chat-send").click()
                page.wait_for_function("!document.querySelector('.pb-chat-send').disabled")
                assert sent and sent[-1]["body"] == "Тест рабочего чата\nВторая строка"
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
                page.screenshot(path=str(OUT / f"thread-{role}-{width}.png"))
                if role == "OWNER" and width == 390:
                    print("QA_PREVIEW thread.jpg " + base64.b64encode(page.screenshot(type="jpeg", quality=60)).decode())
                if width == 320:
                    page.set_viewport_size({"width": 320, "height": 500})
                    expect(page.locator('[data-record="audio"]')).to_be_in_viewport()
                    assert page.locator('.pb-chat-compose').evaluate('e=>e.getBoundingClientRect().bottom<=innerHeight+2')
                    page.set_viewport_size({"width": 320, "height": 844})
                assert page.locator('.pb-chat-dialog').evaluate('(e)=>e.scrollWidth<=e.clientWidth+2')
                if width == 1200:
                    expect(page.locator('.pb-chat-sidebar')).to_be_visible()
                    field.fill('Отправлено клавишей Enter')
                    field.press('Enter')
                    expect(field).to_have_value('')
                    assert sent[-1]['body'] == 'Отправлено клавишей Enter'
                if role == "OWNER":
                    page.locator('[data-message-id="9"] summary').click()
                    page.locator('[data-delete-message="9"]').click()
                    page.locator('[data-delete-cancel]').click()
                    expect(page.locator('[data-message-id="9"]')).to_be_visible()
                    page.locator('[data-message-id="9"] summary').click()
                    page.locator('[data-delete-message="9"]').click()
                    page.locator('[data-delete-confirm]').click()
                    expect(page.locator('[data-message-id="9"]')).to_have_count(0)
                else:
                    assert page.locator('[data-delete-message]').count() == 0
                page.locator('[data-chat="home"]').click()
                page.locator('[data-peer="2"]').click()
                page.locator("#pbChatMessages").wait_for()
                expect(page.locator('.pb-chat-heading .pb-chat-presence')).to_have_text("В сети")
                state["peerOnline"] = False
                # Observe the normal polling cycle, without a page reload.
                expect(page.locator('.pb-chat-heading .pb-chat-presence')).to_have_text("Не в сети", timeout=15000)
                assert state["heartbeat"]["online"] is True
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

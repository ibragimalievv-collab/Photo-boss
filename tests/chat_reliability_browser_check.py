"""Exercise messenger races, pagination and durable sends in a real browser.

Only isolated HTTP fixtures are used; no production messages or staff accounts.
"""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://photo-boss-chat-reliability.invalid"


def message(mid, body, *, sender=2, recipient=None):
    return {"id": mid, "body": body, "senderId": sender,
            "recipientId": recipient, "senderName": "Fixture employee",
            "createdAt": "2026-10-08T11:00:00Z", "attachment": None}


async def wait_state(page, predicate, timeout=5):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("Timed out waiting for browser fixture state")
        await page.wait_for_timeout(20)


async def main():
    state = {"user": 1, "history": [message(i, f"History {i}") for i in range(1, 252)],
             "peer": [message(500, "Private history", recipient=1)], "sent": [],
             "receipts": {}, "reads": [], "queries": [], "hold": None,
             "hold_started": asyncio.Event(), "next_id": 1000}
    errors = []

    async def route(r):
        url = urlsplit(r.request.url)
        path, query = url.path, parse_qs(url.query)
        if path == "/":
            return await r.fulfill(content_type="text/html", body="""<!doctype html>
                <html lang="ru" data-theme="light"><head>
                <meta name="viewport" content="width=device-width,initial-scale=1">
                <link rel="stylesheet" href="/app/css/styles.css"></head><body>
                <header id="topbar" class="topbar"><b>Photo Boss</b></header>
                <script>window.Telegram={WebApp:{initData:"fixture-only"}}</script>
                <script type="module">import * as chat from '/work-chat/chat.js';window.fixtureChat=chat;</script>
                </body></html>""")
        if path.startswith("/work-chat/"):
            file = ROOT / "app/work_chat_ui" / path.rsplit("/", 1)[-1]
        elif path.startswith("/app/"):
            file = ROOT / "app/webapp" / path.removeprefix("/app/")
        else:
            file = None
        if file is not None:
            return await r.fulfill(body=file.read_text(),
                                   content_type="text/css" if path.endswith(".css") else "text/javascript")
        if path == "/api/miniapp/me":
            return await r.fulfill(json={"user": {"id": state["user"], "name": "Fixture",
                                                 "telegramId": 1000 + state["user"], "roles": ["OWNER"]}})
        if path == "/api/miniapp/chat/calls":
            return await r.fulfill(json={"calls": [], "maxParticipants": 6})
        if path == "/api/miniapp/chat/presence":
            return await r.fulfill(json={"online": [2], "ok": True, "ttlSeconds": 45})
        if path == "/api/miniapp/chat/rules":
            return await r.fulfill(json={"accepted": True, "version": "fixture", "sha256": "a" * 64,
                                         "text": "Fixture rules", "retentionDays": 365})
        if path == "/api/miniapp/chat/unread":
            return await r.fulfill(json={"total": 0, "general": 0, "people": {}})
        if path == "/api/miniapp/chat/people":
            return await r.fulfill(json={"people": [{"id": 2, "name": "Fixture peer", "roles": ["PHOTOGRAPHER"], "unread": 0, "online": True}],
                                         "general": {"unread": 0}, "totalUnread": 0, "ownerControl": True})
        if path == "/api/miniapp/chat/read":
            data = json.loads(r.request.post_data)
            state["reads"].append(data)
            assert data["expectedUserId"] == state["user"]
            return await r.fulfill(json={"ok": True, "unread": {"total": 0, "general": 0, "people": {}}})
        if path == "/api/miniapp/chat/messages":
            if r.request.method == "POST":
                data = json.loads(r.request.post_data)
                state["sent"].append(data)
                assert data["expectedUserId"] == state["user"], "A queued send crossed accounts"
                if data["body"] == "Rejected first":
                    return await r.fulfill(status=403, json={"error": "Fixture permission denied"})
                key = (state["user"], data["clientId"])
                if key not in state["receipts"]:
                    mid = state["next_id"]
                    state["next_id"] += 1
                    row = message(mid, data["body"], sender=state["user"], recipient=data["peerId"])
                    state["receipts"][key] = row
                    state["history" if data["peerId"] is None else "peer"].append(row)
                    if data["body"] == "Lost acknowledgement":
                        return await r.abort("failed")
                return await r.fulfill(status=201, json={"message": state["receipts"][key]})
            state["queries"].append(query)
            assert query.get("markRead") == ["0"]
            peer, after = query.get("peer", ["general"])[0], int(query.get("after", [0])[0])
            if peer == "general" and state["hold"] is not None:
                gate = state["hold"]
                state["hold_started"].set()
                await gate.wait()
            rows = [m for m in state["history" if peer == "general" else "peer"] if m["id"] > after]
            return await r.fulfill(json={"messages": rows[:100], "hasMore": len(rows) > 100,
                                         "deletedIds": [], "deletionCursor": 0, "deletedHasMore": False,
                                         "unread": {"total": 0, "general": 0, "people": {}}})
        raise AssertionError("Unexpected fixture request: " + r.request.url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.getenv("CALLS_CHROMIUM_PATH") or None)
        context = await browser.new_context(viewport={"width": 1200, "height": 844})
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.route("**/*", route)
        try:
            await page.goto(BASE)
            await page.locator('[data-open-chat]').click()
            await page.locator('[data-chat="general"]').click()
            await expect(page.locator('[data-message-id]')).to_have_count(251)
            await page.wait_for_function("document.querySelector('[data-message-id=\"251\"]')")
            await wait_state(page, lambda: bool(state["reads"]))
            assert state["reads"][-1]["lastReadMessageId"] == 251
            assert [int(q["after"][0]) for q in state["queries"][:3]] == [0, 100, 200]

            # Hold one request while opening a different conversation: it must not
            # contaminate its history, cursor or read acknowledgement.
            await page.locator('[data-chat="home"]').click()
            state["hold"] = asyncio.Event()
            state["hold_started"].clear()
            reads_before = len(state["reads"])
            await page.locator('[data-chat="general"]').click()
            await asyncio.wait_for(state["hold_started"].wait(), timeout=5)
            await page.locator('[data-peer="2"]').click()
            await expect(page.locator('[data-message-id="500"]')).to_be_visible()
            state["hold"].set()
            state["hold"] = None
            await page.wait_for_timeout(100)
            assert await page.locator('[data-message-id="251"]').count() == 0
            assert all(r["peerId"] == 2 for r in state["reads"][reads_before:])

            # A response arriving after the tab is hidden may refresh history,
            # but must not clear unread state before the user returns.
            await page.locator('[data-chat="home"]').click()
            state["history"].append(message(300, "Arrived while hidden"))
            state["hold"] = asyncio.Event()
            state["hold_started"].clear()
            reads_before = len(state["reads"])
            await page.locator('[data-chat="general"]').click()
            await asyncio.wait_for(state["hold_started"].wait(), timeout=5)
            await page.evaluate("Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'hidden'});document.dispatchEvent(new Event('visibilitychange'))")
            state["hold"].set()
            state["hold"] = None
            await page.wait_for_timeout(100)
            assert len(state["reads"]) == reads_before
            await page.evaluate("Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'visible'});document.dispatchEvent(new Event('visibilitychange'))")
            await expect(page.locator('[data-message-id="300"]')).to_be_visible()
            await wait_state(page, lambda: len(state["reads"]) > reads_before)
            assert state["reads"][-1]["lastReadMessageId"] == 300
            await page.locator('[data-peer="2"]').click()

            # A rejected attachment must remain recoverable in the composer and
            # must not become a permanently blocked send in the durable queue.
            field = page.locator('textarea[name="body"]')
            await field.fill("Keep my caption")
            await page.evaluate("""()=>{const input=document.querySelector('input[name=file]'),d=new DataTransfer();
                d.items.add(new File([new Uint8Array(21*1024*1024)],'too-large.jpg',{type:'image/jpeg'}));
                input.files=d.files;input.dispatchEvent(new Event('change',{bubbles:true}));}""")
            count_before = len(state["sent"])
            await page.locator('.pb-chat-send').click()
            await expect(field).to_have_value("Keep my caption")
            await expect(page.locator('[data-chat-error]')).to_contain_text("20 МБ")
            await expect(page.locator('.pb-chat-pending')).to_have_count(0)
            assert len(state["sent"]) == count_before
            await page.locator('[data-chat="home"]').click()
            await page.locator('[data-peer="2"]').click()
            await expect(page.locator('[data-selected-file]')).to_contain_text("too-large.jpg")
            await expect(field).to_have_value("Keep my caption")
            await page.locator('[data-remove-file]').click()
            await field.fill("")

            # Permanent failure blocks only its own conversation, and a later
            # message cannot silently overtake it. Explicit discard unblocks it.
            await page.locator('[data-chat="general"]').click()
            await field.fill("Rejected first")
            await page.locator('.pb-chat-send').click()
            await expect(page.locator('[data-discard-send]')).to_be_visible()
            await field.fill("Queued second")
            await page.locator('.pb-chat-send').click()
            await expect(page.locator('.pb-chat-pending')).to_have_count(2)
            assert not any(x["body"] == "Queued second" for x in state["sent"])
            await page.locator('[data-peer="2"]').click()
            await field.fill("Other conversation")
            await page.locator('.pb-chat-send').click()
            await expect(page.locator('.pb-chat-bubble').filter(has_text="Other conversation")).to_have_count(1)
            await page.locator('[data-chat="general"]').click()
            await page.locator('[data-discard-send]').click()
            await expect(page.locator('.pb-chat-pending')).to_have_count(0)
            await expect(page.locator('.pb-chat-bubble').filter(has_text="Queued second")).to_have_count(1)

            # Losing only the response must retry the same ID and show one copy.
            await field.fill("Lost acknowledgement")
            await page.locator('.pb-chat-send').click()
            await expect(page.locator('[data-retry-send]')).to_be_visible()
            first = state["sent"][-1]["clientId"]
            await page.locator('[data-retry-send]').click()
            await expect(page.locator('.pb-chat-pending')).to_have_count(0)
            assert state["sent"][-1]["clientId"] == first
            await expect(page.locator('.pb-chat-bubble').filter(has_text="Lost acknowledgement")).to_have_count(1)

            # Locally stored sends and drafts survive account changes without
            # leaking into the next user's UI or being sent under their cookie.
            await field.fill("Private unsent draft")
            await page.evaluate("Object.defineProperty(navigator,'onLine',{configurable:true,get:()=>false});window.dispatchEvent(new Event('offline'))")
            await field.fill("Offline user one")
            await page.locator('.pb-chat-send').click()
            await expect(page.locator('.pb-chat-pending')).to_have_count(1)
            sends_before = len(state["sent"])
            await field.fill("Private unsent draft")
            await page.locator('[data-chat="home"]').click()
            await expect(page.locator('[data-peer="2"]')).to_be_visible()
            await page.locator('[data-peer="2"]').click()
            await field.fill("Offline peer one")
            await page.locator('.pb-chat-send').click()
            await expect(page.locator('.pb-chat-pending')).to_have_count(1)
            assert len(state["sent"]) == sends_before
            await page.locator('[data-chat="home"]').click()
            await page.locator('[data-chat="general"]').click()
            await expect(field).to_have_value("Private unsent draft")
            await expect(page.locator('.pb-chat-pending')).to_have_count(1)
            await page.evaluate("document.dispatchEvent(new Event('pb-chat-account-reset'))")
            state["user"] = 7
            await page.evaluate("""async()=>{Object.defineProperty(navigator,'onLine',{configurable:true,get:()=>true});
                document.dispatchEvent(new CustomEvent('pb-chat-account-ready',{detail:{user:{id:7,telegramId:1007,name:'Second fixture',roles:['ADMIN']}}}));
                window.dispatchEvent(new Event('online'));}""")
            await page.locator('[data-open-chat]').click()
            await page.locator('[data-chat="general"]').click()
            await expect(field).to_have_value("")
            await expect(page.locator('.pb-chat-pending')).to_have_count(0)
            assert len(state["sent"]) == sends_before
            await page.evaluate("document.dispatchEvent(new Event('pb-chat-account-reset'))")
            state["user"] = 1
            await page.evaluate("document.dispatchEvent(new CustomEvent('pb-chat-account-ready',{detail:{user:{id:1,telegramId:1001,name:'First fixture',roles:['OWNER']}}}))")
            await page.locator('[data-open-chat]').click()
            await page.locator('[data-chat="general"]').click()
            await expect(field).to_have_value("Private unsent draft")
            await expect(page.locator('.pb-chat-bubble').filter(has_text="Offline user one")).to_have_count(1)
            await wait_state(page, lambda: len(state["sent"]) == sends_before + 2)
            assert [x["peerId"] for x in state["sent"][sends_before:]] == [None, 2]
            assert all(x["expectedUserId"] == 1 for x in state["sent"][sends_before:])
            await page.locator('[data-peer="2"]').click()
            await expect(page.locator('.pb-chat-bubble').filter(has_text="Offline peer one")).to_have_count(1)
            assert not errors, errors
            print("Chat pagination, switch race, attachment recovery, retry order, dedupe and account isolation passed.")
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

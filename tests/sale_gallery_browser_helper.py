"""Optional real Chromium/UI leg for sale_gallery_provider_check.py.

No application services or API responses are mocked here. Only the external
Telegram bootstrap script and unrelated side-effect UI modules are suppressed.
Every sale uses the actual app.js/workflow.js/outbox.js and production routes.
"""

import asyncio
import hashlib
import json
import os
import ssl
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def local_tls():
    """Temporary localhost certificate; never changes the deployed service."""
    directory = tempfile.TemporaryDirectory(prefix="pb-provider-tls-")
    root = Path(directory.name)
    cert, key = root / "cert.pem", root / "key.pem"
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        return directory, context
    except BaseException:
        directory.cleanup()
        raise


class BrowserSales:
    def __init__(self, base, signed_init, require, profile, expected_date, *, partial_preflight=None):
        self.base, self.signed_init, self.require, self.profile = base, signed_init, require, profile
        self.playwright = self.browser = None
        self.expected_date = expected_date
        self.partial_preflight = partial_preflight
        self.contexts, self.pages, self.rows = {}, {}, {}
        self.page_errors = []
        self.concurrent_ready = 0
        self.concurrent_save = asyncio.Event()
        self.report = {"profile": profile, "save_clicks": 0, "sales": 0,
                       "same_filename_photos": True, "lost_ack_recovered": False,
                       "durable_ui_operations": 0, "inline_new_guest_sales": 0,
                       "concurrent_contexts": 2 if profile == "concurrent" else 1,
                       "double_click_attempted": profile == "desktop"}

    async def start(self):
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            executable_path=os.environ.get("PB_PROVIDER_CHROMIUM_PATH") or os.environ.get("CALLS_CHROMIUM_PATH") or None,
            headless=True)

    async def submit(self, n, booking_id, selected, receipt):
        from playwright.async_api import expect
        self.report["last_ui_step"] = f"sale_{n + 1}_open_form"
        viewport = {"width": 1440, "height": 1000} if self.profile == "desktop" else {"width": 390, "height": 844}
        context = await self.browser.new_context(ignore_https_errors=True, viewport=viewport,
                                                 accept_downloads=True, service_workers="block")
        self.contexts[n] = context
        await context.add_init_script("window.Telegram={WebApp:{initData:" + json.dumps(self.signed_init) + "}}")
        async def only_local(route):
            target = urlsplit(route.request.url)
            if target.scheme in {"http", "https"} and target.netloc != urlsplit(self.base).netloc:
                await route.abort("blockedbyclient")
            else:
                await route.continue_()
        await context.route("**/*", only_local)
        # More specific routes installed last take precedence in Playwright.
        await context.route("https://telegram.org/**", lambda route: route.fulfill(body="", content_type="text/javascript"))
        # These entry modules belong to separate render_main installers and do
        # not participate in the sale. The real sale UI is served unmodified.
        for prefix in ("shift", "people", "work-chat"):
            await context.route(f"**/{prefix}/**", lambda route: route.fulfill(body="", content_type="text/javascript"))
        fault = {"selected": 0, "injected": False}
        lost_ack = asyncio.Event()
        if self.profile == "mobile-recovery" and n == 0:
            async def lose_final_photo_ack(route):
                body = route.request.post_data_buffer or b""
                if b'"kind":"sale_selected"' in body and not fault["injected"]:
                    fault["selected"] += 1
                    if fault["selected"] == len(selected):
                        response = await route.fetch()
                        self.require(response.status == 200, "lost_ack_server_acceptance")
                        self.report["photo_acks_before_loss"] = fault["selected"] - 1
                        fault["injected"] = True
                        await route.abort("failed")
                        lost_ack.set()
                        return
                await route.continue_()
            await context.route("**/api/miniapp/operations/media", lose_final_photo_ack)
        page = await context.new_page()
        self.pages[n] = page
        page.on("pageerror", lambda error: self.page_errors.append(type(error).__name__))
        await page.goto(self.base + "/app/#workflow")
        await page.locator("#workflowSalePick").select_option(str(booking_id) if booking_id is not None else "-1")
        form = page.locator("#workflowSale")
        if booking_id is None:
            await form.locator("[name=guest_name]").fill("Synthetic Guest One")
            hotel = form.locator("[name=hotel_id]")
            if await hotel.evaluate("element=>element.tagName==='SELECT'"):
                await hotel.select_option("1")
            await expect(hotel).to_have_value("1")
            await form.locator("[name=photographer_id]").select_option("2")
            await expect(form.locator("[name=photographer_id] option:checked")).to_have_text("Synthetic Photographer")
        else:
            context_card = form.locator(".sale-context")
            for label in ("Synthetic Guest One", "Synthetic Diagnostic Hotel", "Synthetic Photographer",
                          self.expected_date, "DIAG-0", "TEST-CONTACT-ONLY"):
                await expect(context_card).to_contain_text(label)
            self.report["existing_booking_context_without_reentry"] = True
        await form.locator("[name=selected]").set_input_files([
            {"name": "selected.png", "mimeType": "image/png", "buffer": raw} for raw in selected])
        await expect(form.locator("[name=sold]")).to_have_value(str(len(selected)))
        await expect(form.locator("[name=total]")).to_have_value(str(len(selected)) if booking_id is None else "3")
        button = form.get_by_role("button", name="Сохранить продажу", exact=True)
        await expect(button).to_have_count(1)
        if self.partial_preflight is not None and n == 0:
            self.report["last_ui_step"] = "partial_deposit_reject_without_new_receipt"
            await button.click()
            self.report["save_clicks"] += 1
            await expect(page.locator("#toast")).to_contain_text("Прикрепите чек продажи на остаток к оплате")
            await expect(button).to_be_enabled()
            queue = await page.evaluate("async()=>await (await import('/app/js/outbox.js')).outboxRows()")
            self.require(not queue and not await form.get_attribute("data-pending-keys"),
                         "partial_deposit_preflight_does_not_enqueue")
            retained = await form.locator("[name=selected]").evaluate("""async element=>
              Promise.all([...element.files].map(async file=>{
                const bytes=await file.arrayBuffer(),sha=await crypto.subtle.digest('SHA-256',bytes);
                return [...new Uint8Array(sha)].map(n=>n.toString(16).padStart(2,'0')).join('');
              }))""")
            self.require(retained == [hashlib.sha256(raw).hexdigest() for raw in selected],
                         "partial_deposit_preflight_preserves_selected_files")
            await self.partial_preflight()
            self.report["partial_deposit_no_receipt_preflight"] = True
            self.report["selected_files_preserved_after_preflight"] = len(retained)
        await form.locator("[name=receipt]").set_input_files(
            {"name": "receipt.png", "mimeType": "image/png", "buffer": receipt})
        self.require(await form.locator("[data-sale-client-link]").count() == 0, "ui_no_guest_link_before_save")
        if self.profile == "concurrent":
            self.concurrent_ready += 1
            if self.concurrent_ready == 2:
                self.report["concurrent_save_barrier"] = True
                self.concurrent_save.set()
            await asyncio.wait_for(self.concurrent_save.wait(), 30)
        if self.profile == "desktop":
            await button.dblclick()
            self.report["save_clicks"] += 2
        else:
            await button.click()
            self.report["save_clicks"] += 1
        if self.profile == "mobile-recovery" and n == 0:
            await asyncio.wait_for(lost_ack.wait(), 120)
            # The accepted HTTP request lost its response. Refresh from durable
            # IndexedDB and let the real outbox retry the same operation key.
            await page.reload()
        self.report["last_ui_step"] = f"sale_{n + 1}_wait_server_save_ack"
        await expect(form.locator("[data-operation-progress]")).to_contain_text("сохранена сервером", timeout=180000)
        await expect(form.locator("[data-sale-gallery]")).to_be_visible()
        self.require(await form.locator("[data-sale-client-link]").count() == 0, "ui_pending_payment_has_no_guest_link")
        rows = await page.evaluate("async()=>await (await import('/app/js/outbox.js')).outboxRows()")
        self.require(rows and all(row["status"] == "synced" for row in rows), "ui_durable_queue_fully_acknowledged")
        self.require(len([row for row in rows if row["kind"] == "sale_selected"]) == len(selected), "ui_exact_selected_file_count")
        self.require(len([row for row in rows if row["kind"] == "sale_complete"]) == 1, "ui_one_final_complete_operation")
        self.require(len([row for row in rows if row["kind"] in {"sale_start", "sale_start_inline"}]) == 1,
                     "ui_one_start_operation_after_save")
        self.rows[n] = rows
        self.report["sales"] += 1
        self.report["durable_ui_operations"] += len(rows)
        self.report["inline_new_guest_sales"] += len([row for row in rows if row["kind"] == "sale_start_inline"])
        if fault["injected"]:
            self.report["lost_ack_recovered"] = True
        return rows

    async def paid_gallery(self, n, selected):
        from playwright.async_api import expect
        self.report["last_ui_step"] = f"sale_{n + 1}_paid_status_refresh"
        page, form = self.pages[n], self.pages[n].locator("#workflowSale")
        # This is the actual status button on the saved sale, after the isolated
        # receipt allocator has changed its payment status on the server.
        button = form.locator('[data-workflow="gallery-status"]')
        if await button.count():
            await button.click()
        link = form.locator("[data-sale-client-link]")
        self.report["last_ui_step"] = f"sale_{n + 1}_wait_paid_guest_link"
        await expect(link).to_be_visible(timeout=30000)
        url = await link.input_value()
        self.require(urlsplit(url).netloc == urlsplit(self.base).netloc, "ui_guest_capability_localhost_only")
        await form.locator('[data-workflow="gallery-qr"]').click()
        self.report["last_ui_step"] = f"sale_{n + 1}_wait_qr_png"
        qr = form.locator(".sale-gallery-qr")
        await expect(qr).to_be_visible()
        await expect(qr).to_have_js_property("complete", True)
        self.require(await qr.evaluate("image=>image.naturalWidth>0"), "ui_qr_png_loaded")
        guest_context = await self.browser.new_context(ignore_https_errors=True, accept_downloads=True, service_workers="block")
        async def guest_origin_only(route):
            target = urlsplit(route.request.url)
            if target.scheme in {"http", "https"} and target.netloc != urlsplit(self.base).netloc:
                await route.abort("blockedbyclient")
            else:
                await route.continue_()
        await guest_context.route("**/*", guest_origin_only)
        try:
            guest = await guest_context.new_page()
            self.report["last_ui_step"] = f"sale_{n + 1}_guest_browser_checksums"
            await guest.goto(url)
            # Fetch from the actual guest page's origin, without employee auth.
            checksums = await guest.evaluate("""async()=>{
              const data=await (await fetch(location.pathname+'/data')).json();
              return await Promise.all(data.photos.map(async photo=>{
                const response=await fetch(photo.url+'?download=1');
                const bytes=await response.arrayBuffer();
                const sha=await crypto.subtle.digest('SHA-256',bytes);
                return [...new Uint8Array(sha)].map(n=>n.toString(16).padStart(2,'0')).join('');
              }));
            }""")
            self.require(checksums == [hashlib.sha256(raw).hexdigest() for raw in selected], "browser_guest_download_checksums")
        finally:
            await guest_context.close()
        self.require(not self.page_errors, "browser_uncaught_page_error")
        self.report["last_ui_step"] = f"sale_{n + 1}_ui_guest_flow_complete"
        evidence = os.environ.get("PB_PROVIDER_BROWSER_EVIDENCE")
        if evidence:
            directory = Path(evidence)
            directory.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(directory / f"{self.profile}-sale-{n + 1}.png"), full_page=True)

    async def close(self):
        for context in self.contexts.values():
            await context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

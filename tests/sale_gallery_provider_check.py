"""Explicit, disposable sale → private provider → guest gallery diagnostic.

Run from the application checkout, with its installed requirements::

    PB_PROVIDER_DIAG=1 PB_PROVIDER_FAKE=1 PB_PROVIDER_DATABASE_URL=... \
        python tests/sale_gallery_provider_check.py

Optional actual UI cases (Playwright, Chromium/headless shell and openssl are
required only for this mode): add PB_PROVIDER_BROWSER=1 and one of
PB_PROVIDER_CASE=desktop, mobile-recovery, concurrent. Each process uses a fresh
UUID schema and new private browser contexts. Desktop attempts double-click;
mobile-recovery loses the third photo acknowledgement and reloads IndexedDB;
concurrent saves independent bookings with the same guest and file names.

For the real-provider run omit PB_PROVIDER_FAKE. DATABASE_URL and the existing
YANDEX_DISK_TOKEN stay in the server environment. Never paste either value into
this file or command history. This script prints allowlisted evidence only.

All rows live in a fresh UUID schema, including rows accessed through app.db's
global Session and operations_guard's separate lease engine. Production startup,
workers, bank, AI, Telegram and push delivery are not started. Private synthetic
files live only under the UUID diagnostics directory, removed during cleanup.
"""

# Driver/provider exception messages may contain secrets. Every failure must
# still attempt both cleanups and emit only its type or an allowlisted label.
# ruff: noqa: BLE001

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import sys
import time
import warnings
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit
from uuid import uuid4


class CheckFailed(RuntimeError):
    """A non-sensitive assertion label suitable for diagnostic output."""


def require(value, label):
    if not value:
        raise CheckFailed(label)


def signed(user_id, token):
    values = {"auth_date": str(int(time.time())), "user": json.dumps(
        {"id": user_id, "first_name": "Synthetic"}, separators=(",", ":"))}
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    body = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    values["hash"] = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


async def run():
    # Refuse before importing the application, opening a DB connection or using
    # provider credentials. The caller must explicitly opt into a write probe.
    require(os.environ.get("PB_PROVIDER_DIAG") == "1", "explicit_diag_mode_required")
    fake = os.environ.get("PB_PROVIDER_FAKE") == "1"
    browser_mode = os.environ.get("PB_PROVIDER_BROWSER") == "1"
    browser_case = os.environ.get("PB_PROVIDER_CASE", "desktop")
    partial_deposit = os.environ.get("PB_PROVIDER_PARTIAL_DEPOSIT") == "1"
    require(not browser_mode or browser_case in {"desktop", "mobile-recovery", "concurrent"}, "unknown_browser_case")
    require(not partial_deposit or (browser_mode and browser_case == "desktop"), "partial_deposit_requires_desktop_browser")
    database_url = os.environ.get("PB_PROVIDER_DATABASE_URL") or os.environ.get("DATABASE_URL")
    require(database_url, "database_environment_required")
    require(fake or bool(os.environ.get("YANDEX_DISK_TOKEN", "").strip()), "provider_environment_required")
    require(not any(name == "app" or name.startswith("app.") for name in sys.modules),
            "fresh_python_process_required")

    logging.disable(logging.CRITICAL)
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    # config.py loads dotenv at import time. In this disposable process no local
    # file may re-enable a service which has just been explicitly disabled.
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    token = "123456789:provider-diagnostic-fake-bot-token"
    os.environ.update(DATABASE_URL=database_url, BOT_TOKEN=token,
        ADMIN_TELEGRAM_IDS="1", OPENAI_API_KEY="", ANTHROPIC_API_KEY="",
        GEMINI_API_KEY="", REDIS_URL="", TELEGRAM_API_BASE="",
        SBER_PAYMENTS_ENABLED="0", SBER_QR_ENABLED="0", SBER_MERCHANT_LOGIN="",
        SBER_MERCHANT_PASSWORD="", WEB_PUSH_PUBLIC_KEY="", WEB_PUSH_PRIVATE_KEY="",
        WEB_PUSH_SUBJECT="", PHOTO_BOSS_NOTIFICATION_CHANNEL="app",
        WEBHOOK_BASE_URL="https://provider-diag.invalid", PYTHON_DOTENV_DISABLED="1")
    if fake:
        os.environ.update(YANDEX_DISK_TOKEN="synthetic-provider-only", YANDEX_DISK_READ_TOKEN="", YANDEX_DISK_CLIENT_ID="")

    import sqlalchemy.ext.asyncio as sa_async
    from sqlalchemy import event, func, select, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import AsyncSession

    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+asyncpg")
    require(url.drivername == "postgresql+asyncpg", "postgres_asyncpg_required")
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    uid = uuid4().hex
    schema = "pb_sale_flow_" + uid
    sentinel = uuid4().hex
    prefix = "app:/PhotoBoss/system/diagnostics/sale-flow-" + uid
    phase = "isolation_setup"
    report = {"ok": False, "fake_provider": fake, "schema": schema,
              "private_prefix": prefix, "checks": [], "cleanup": {}}
    engines = []
    isolation = {"ready": False, "connect_checks": 0}
    original_create = sa_async.create_async_engine

    async def verify_driver(connection):
        row = await connection.fetchrow(
            "SELECT current_schema() AS schema, current_setting('search_path') AS path, "
            "(SELECT marker FROM _pb_diag_sentinel) AS marker")
        require(row["schema"] == schema and row["path"] == schema and row["marker"] == sentinel,
                "connection_schema_sentinel_mismatch")
        isolation["connect_checks"] += 1

    def isolated_engine(target, **kwargs):
        require(make_url(target) == url, "unexpected_database_target")
        connect_args = dict(kwargs.pop("connect_args", {}) or {})
        settings = dict(connect_args.get("server_settings", {}) or {})
        # No public fallback, including engines created lazily by operation_lease.
        settings.update(search_path=schema, application_name=schema)
        connect_args["server_settings"] = settings
        kwargs.update(connect_args=connect_args, echo=False, hide_parameters=True)
        engine = original_create(target, **kwargs)
        engines.append(engine)

        @event.listens_for(engine.sync_engine, "connect")
        def check_connection(dbapi_connection, connection_record):
            if isolation["ready"]:
                dbapi_connection.run_async(verify_driver)

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def reject_unsafe_sql(connection, cursor, statement, parameters, context, executemany):
            # search_path protects unqualified SQL. Reject explicit public writes
            # and DB/role changes too, rather than trusting application routing.
            require(not re.search(r'(?i)(?:"?public"?\s*\.|\bALTER\s+(?:ROLE|DATABASE)\b|\bSET\s+search_path\b)', statement),
                    "unsafe_sql_outside_disposable_schema")
        return engine

    # Patch before *any* app import: all from-import aliases inherit the wrapper.
    sa_async.create_async_engine = isolated_engine
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    client = None
    browser_driver = tls_directory = None
    schema_created = False
    storage = None
    provider_cleanup_allowed = False
    failure = None
    try:
        from unittest.mock import AsyncMock

        from aiohttp import FormData, TCPConnector, web
        from aiohttp.test_utils import TestClient, TestServer
        from PIL import Image
        from sale_gallery_provider_cleanup import delete_diagnostic_uuid_directory

        from app import db, gallery_folders, workflow, yandex_disk
        from app.miniapp_api import install_miniapp
        from app.models import (
            Booking,
            Client,
            DeliveryGallery,
            DeliveryPhoto,
            Hotel,
            HotelEmployee,
            OperationRequest,
            Package,
            PayrollEntry,
            Photo,
            Receipt,
            Sale,
            SaleDraft,
            SaleDraftPhoto,
            Shooting,
            User,
            UserRole,
            utc_now,
        )
        from app.services import media_files
        from app.services.receipts import refresh_payment_statuses

        engine = isolated_engine(url, pool_size=3, max_overflow=2, pool_timeout=20)
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text('CREATE TABLE _pb_diag_sentinel (marker TEXT NOT NULL)'))
            await connection.execute(text('INSERT INTO _pb_diag_sentinel VALUES (:marker)'), {"marker": sentinel})
        schema_created = True
        isolation["ready"] = True
        async with engine.begin() as connection:
            require(await connection.scalar(text('SELECT current_schema()')) == schema, "api_engine_schema")
            await connection.run_sync(db.Base.metadata.create_all)
        async with db.Session() as session:
            require(await session.scalar(text('SELECT marker FROM _pb_diag_sentinel')) == sentinel,
                    "global_session_schema")
        report["checks"].append("api_and_global_session_schema_isolated")
        try:
            async with engine.connect() as connection:
                await connection.execute(text('SELECT 1 FROM public.users LIMIT 0'))
        except CheckFailed as exc:
            require(str(exc) == "unsafe_sql_outside_disposable_schema", "public_guard_error")
        else:
            raise CheckFailed("explicit_public_sql_was_not_blocked")
        report["checks"].append("explicit_public_sql_rejected_before_execution")

        original_disk = yandex_disk.YandexDisk
        provider = {"uploads": 0, "downloads": 0, "files": {}, "dirs": set(), "hashes": set()}

        def private_path(path, *, ancestor=False):
            require(isinstance(path, str) and (path == prefix or path.startswith(prefix + "/")
                or (ancestor and prefix.startswith(path + "/"))), "provider_path_outside_diagnostics")
            yandex_disk.safe_path(path)
            return path

        class DiagnosticDisk(original_disk):
            async def ensure_dir(self, path):
                private_path(path, ancestor=True)
                if fake:
                    provider["dirs"].add(path)
                    return
                return await super().ensure_dir(path)

            async def metadata(self, path):
                # ensure_dir verifies an existing ancestor after provider 409.
                # Reading that exact ancestor directory is allowed; originals
                # and every upload/download remain restricted to the UUID root.
                private_path(path, ancestor=True)
                if not fake:
                    return await super().metadata(path)
                if path in provider["files"]:
                    raw = provider["files"][path]
                    return {"type": "file", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                if path in provider["dirs"]:
                    return {"type": "dir"}
                raise yandex_disk.YandexDiskError("Yandex.Disk HTTP 404", http_status=404)

            async def upload_bytes(self, path, raw, **kwargs):
                private_path(path)
                provider["uploads"] += 1
                if fake:
                    if path.rsplit("/", 1)[0] not in provider["dirs"]:
                        raise yandex_disk.YandexDiskError("Yandex.Disk HTTP 409", http_status=409)
                    provider["files"][path] = bytes(raw)
                    yandex_disk.validate_saved(await self.metadata(path), raw)
                else:
                    await super().upload_bytes(path, raw, **kwargs)
                provider["hashes"].add(hashlib.sha256(raw).hexdigest())

            async def download_bytes(self, path, **kwargs):
                private_path(path)
                provider["downloads"] += 1
                return provider["files"][path] if fake else await super().download_bytes(path, **kwargs)

            async def delete(self, path):
                private_path(path)
                require(path == prefix, "only_uuid_root_cleanup_allowed")
                if fake:
                    provider["files"].clear()
                    provider["dirs"].clear()
                    return
                await super().delete(path)

        # Also replace aliases already imported by the production installers.
        # yandex_disk.ROOT/safe_path retain their production namespace validator.
        for module in tuple(sys.modules.values()):
            if (module and getattr(module, "__name__", "").startswith("app.")
                    and getattr(module, "YandexDisk", None) is original_disk):
                module.YandexDisk = DiagnosticDisk
        gallery_folders.ROOT = workflow.ROOT = media_files.ROOT = prefix
        storage = DiagnosticDisk(*yandex_disk.configured_from_env())
        expected_login = os.environ.get("PB_PROVIDER_EXPECTED_LOGIN", "").strip()
        if not fake and expected_login:
            phase = "provider_account_guard"
            account = await storage._json("GET", "")
            require(isinstance(account, dict) and isinstance(account.get("user"), dict)
                    and account["user"].get("login") == expected_login, "unexpected_provider_account")
            report["expected_account_matches"] = True
        # A failed account guard must not even DELETE on the wrong account.
        provider_cleanup_allowed = True
        cursor = "app:/PhotoBoss"
        await storage.ensure_dir(cursor)
        for part in prefix[len(cursor) + 1:].split("/"):
            cursor += "/" + part
            await storage.ensure_dir(cursor)

        def image(seed):
            raw = random.Random(seed).randbytes(128 * 96 * 3)
            output = io.BytesIO()
            Image.frombytes("RGB", (128, 96), raw).save(output, format="PNG")
            return output.getvalue()

        photos = [image(n) for n in range(3)]
        for action in (lambda: storage.upload_bytes("app:/PhotoBoss/outside-diag.png", photos[0]),
                       lambda: storage.delete(prefix + "/child")):
            try:
                await action()
            except CheckFailed:
                pass
            else:
                raise CheckFailed("provider_path_guard_not_enforced")
        report["checks"].append("outside_provider_writes_and_non_root_delete_rejected")
        today = utc_now().date()
        from datetime import time as clock_time
        from datetime import timedelta
        async with AsyncSession(engine, expire_on_commit=False) as session:
            session.add_all([User(id=1, tg_id=1, name="Synthetic Owner"),
                User(id=2, tg_id=2, name="Synthetic Photographer"),
                User(id=3, tg_id=3, name="Synthetic Manager"),
                User(id=4, tg_id=4, name="Synthetic Unrelated Employee"),
                Hotel(id=1, name="Synthetic Diagnostic Hotel"),
                Package(id=1, name="Synthetic Diagnostic", price_per_photo=400),
                Client(id=1, name="Synthetic Guest One", phone="TEST-CONTACT-ONLY"),
                Client(id=2, name="Synthetic Guest One" if browser_case == "concurrent" else "Synthetic Guest Two")])
            await session.flush()
            session.add_all([UserRole(user_id=1, role="OWNER"),
                UserRole(user_id=2, role="PHOTOGRAPHER"), UserRole(user_id=3, role="MANAGER"),
                UserRole(user_id=4, role="MANAGER")])
            session.add_all([HotelEmployee(user_id=3, hotel_id=1), HotelEmployee(user_id=2, hotel_id=1)])
            bookings = []
            for n in range(2):
                booking = Booking(hotel_id=1, client_id=n + 1, package_id=1, manager_id=3,
                    photographer_id=2, room=f"DIAG-{n}", deposit=0, shoot_date=today,
                    shoot_time=clock_time(12 + n), status="READY_FOR_SALE")
                session.add(booking)
                await session.flush()
                shooting = Shooting(booking_id=booking.id, status="READY_FOR_SALE",
                                    full_upload_completed_at=utc_now())
                session.add(shooting)
                await session.flush()
                session.add_all(Photo(shooting_id=shooting.id, file_id=f"synthetic-full-{n}-{i}") for i in range(3))
                bookings.append(booking.id)
            if partial_deposit:
                deposit_hash = hashlib.sha256(b"synthetic-approved-deposit-fixture").hexdigest()
                session.add(Receipt(booking_id=bookings[0], uploaded_by_id=3, purpose="DEPOSIT",
                    file_id="fixture:synthetic-approved-deposit", file_unique_id="fixture:synthetic-approved-deposit",
                    image_sha256=deposit_hash, approved_image_hash=deposit_hash,
                    expected_amount=400, verified_amount=400, status="APPROVED",
                    reviewed_by_id=1, reviewed_at=utc_now(), analysis=json.dumps({"status": "synthetic_fixture"})))
            await session.commit()
        async with engine.begin() as connection:
            for table in ("users", "clients", "hotels", "packages"):
                await connection.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}','id'), (SELECT max(id) FROM {table}))"))

        blocked = AsyncMock(side_effect=CheckFailed("external_bot_delivery_attempted"))
        bot = SimpleNamespace(token=token, send_message=blocked, send_photo=blocked,
            send_document=blocked, download=blocked, me=AsyncMock(
                return_value=SimpleNamespace(username="SyntheticDiagnosticBot")))
        served = {"requests": 0}
        @web.middleware
        async def count_business_requests(request, handler):
            if request.path.startswith(("/api/miniapp/", "/g/")):
                served["requests"] += 1
            return await handler(request)
        app = web.Application(client_max_size=25 * 1024 * 1024, middlewares=[count_business_requests])
        install_miniapp(app, engine=engine, bot=bot, lessons=[])
        client = TestClient(TestServer(app, host="127.0.0.1"), connector=TCPConnector(ssl=False) if browser_mode else None)
        server_options = {}
        if browser_mode:
            from sale_gallery_browser_helper import BrowserSales, local_tls
            tls_directory, server_options["ssl"] = local_tls()
        if server_options:
            await client.server.start_server(**server_options)
        await client.start_server()
        if browser_mode:
            async def partial_preflight():
                async with db.Session() as session:
                    for model in (OperationRequest, SaleDraft, Sale, SaleDraftPhoto):
                        require(await session.scalar(select(func.count(model.id))) == 0,
                                "partial_deposit_no_server_operations_before_receipt")
                report["checks"].append("partial_deposit_preflight_before_any_server_operations")
            base = str(client.make_url("/")).rstrip("/")
            os.environ["WEBHOOK_BASE_URL"] = base
            browser_driver = BrowserSales(base, signed(3, token), require, browser_case, str(today),
                                           partial_preflight=partial_preflight if partial_deposit else None)
            await browser_driver.start()
        request_count = 0
        browser_rows = {}

        async def request(method, path, *, actor=None, status=200, extra_headers=None,
                          response_meta=False, **kwargs):
            nonlocal request_count
            require(path.startswith("/") and not path.startswith("//"), "localhost_requests_only")
            headers = {"X-Telegram-Init-Data": signed(actor, token)} if actor else {}
            headers.update(extra_headers or {})
            async with client.session.request(method, client.make_url(path), headers=headers, **kwargs) as response:
                raw = await response.read()
                request_count += 1
                require(response.status == status, f"http_status_{response.status}_expected_{status}")
                if response.headers.get("Content-Type", "").startswith("application/json"):
                    raw = json.loads(raw)
                return (raw, dict(response.headers)) if response_meta else raw

        async def operation(n, kind, data, raw=None, suffix=""):
            nonlocal phase
            phase = f"sale_{n + 1}_{kind}{suffix}"
            key = "provider-diag-" + hashlib.sha256(f"{uid}:{n}:{kind}:{suffix}".encode()).hexdigest()
            packet = {"actorId": 3, "key": key,
                      "kind": kind, "date": str(today), "data": data}
            if n in browser_rows and (not suffix or (kind == "sale_selected" and re.fullmatch(r"-\d+", suffix))):
                candidates = [row for row in browser_rows[n] if row["kind"] == kind
                              or (kind == "sale_start" and row["kind"] == "sale_start_inline")]
                row = candidates[int(suffix[1:]) if suffix else 0]
                packet = {"actorId": row["userId"], "key": row["key"], "kind": row["kind"],
                          "date": row["date"], "data": row["data"]}
                if "generation" in row:
                    packet["generation"] = row["generation"]
                if row.get("salesGeneration"):
                    packet["salesGeneration"] = row["salesGeneration"]
            if raw is None:
                return await request("POST", "/api/miniapp/operations/sync", actor=3, json=packet)
            form = FormData()
            form.add_field("operation", json.dumps(packet))
            form.add_field("file", raw, filename="synthetic.png", content_type="image/png")
            return await request("POST", "/api/miniapp/operations/media", actor=3, data=form)

        async def workflow_gallery(booking_id):
            data = await request("GET", f"/api/miniapp/workflow?booking={booking_id}", actor=3)
            return next(b["gallery"] for b in data["bookings"] if b["id"] == booking_id)

        async def identities():
            async with AsyncSession(engine) as session:
                return {model.__tablename__: list(await session.scalars(select(model.id).order_by(model.id)))
                    for model in (Sale, Receipt, PayrollEntry, DeliveryGallery, DeliveryPhoto, SaleDraftPhoto)}

        journeys = []
        storage_hierarchy = set()
        selections = (photos if browser_mode and browser_case == "mobile-recovery" else photos[1:], photos[:1])
        if browser_mode and browser_case == "concurrent":
            phase = "concurrent_browser_saves"
            results = await asyncio.gather(*(browser_driver.submit(n, bookings[n] if n == 0 else None, selected, photos[n * 2])
                                             for n, selected in enumerate(selections)))
            browser_rows.update(enumerate(results))
        for n, selected in enumerate(selections):
            phase = f"sale_{n + 1}_http"
            booking_id = bookings[n]
            if browser_mode and n not in browser_rows:
                phase = f"sale_{n + 1}_browser_save"
                browser_rows[n] = await browser_driver.submit(n, booking_id if n == 0 else None, selected, photos[n * 2])
            start = await operation(n, "sale_start", {"booking": {"id": booking_id}, "price": "400.00"})
            draft_id = start["draftId"]
            booking_id = bookings[n] = start["bookingId"]
            require((await operation(n, "sale_start", {"booking": {"id": booking_id}, "price": "400.00"}))["draftId"] == draft_id,
                    "start_replay_draft_identity")
            ref = {"draft": {"id": draft_id}}
            receipt_raw = photos[n * 2]
            if not browser_mode:
                await operation(n, "sale_receipt", ref, receipt_raw)
            received = await request("GET", f"/api/miniapp/sale-drafts/{draft_id}/receipt/image", actor=3)
            require(received == receipt_raw, "receipt_provider_roundtrip")
            if not browser_mode:
                await operation(n, "sale_counts", ref | {"total": 3, "sold": len(selected), "price": "400.00"})
            last = None
            for i, raw in enumerate(selected):
                last = ([row["result"] for row in browser_rows[n] if row["kind"] == "sale_selected"][i]
                        if browser_mode else await operation(n, "sale_selected", ref, raw, suffix=f"-{i}"))
                again = await operation(n, "sale_selected", ref, raw, suffix=f"-{i}")
                require(again["photoId"] == last["photoId"], "selected_replay_photo_identity")
            finished = await operation(n, "sale_complete", ref | {"price": "400.00"})
            require(last["saleId"] == finished["saleId"], "auto_complete_sale_identity")
            pending = finished["gallery"]
            require(pending["state"] == "payment_pending" and not pending["ready"]
                    and pending["clientUrl"] is None and pending["photoCount"] == len(selected),
                    "pending_ack_truth")
            require((await workflow_gallery(booking_id))["state"] == "payment_pending", "pending_workflow_truth")
            await request("GET", f'/api/miniapp/sales/{finished["saleId"]}/gallery-qr', actor=3, status=409)
            await request("GET", f'/api/miniapp/sales/{finished["saleId"]}/gallery-qr', actor=4, status=403)
            async with AsyncSession(engine) as session:
                gallery = await session.scalar(select(DeliveryGallery).where(DeliveryGallery.booking_id == booking_id))
                guest = "/g/" + gallery.access_token
                linked = list(await session.scalars(select(DeliveryPhoto).where(DeliveryPhoto.gallery_id == gallery.id).order_by(DeliveryPhoto.id)))
                require(len(linked) == len(selected) and all(p.origin == "SALE" for p in linked), "sale_photo_lineage")
                photo_ids = [p.id for p in linked]
                actual_booking = await session.get(Booking, booking_id)
                if browser_mode and n == 1:
                    inline_shooting = await session.scalar(select(Shooting).where(Shooting.booking_id == booking_id))
                    require(inline_shooting is not None and inline_shooting.full_upload_completed_at is None
                            and await session.scalar(select(func.count(Photo.id)).where(Photo.shooting_id == inline_shooting.id)) == 0,
                            "inline_no_fabricated_full_photo_upload")
                    require(actual_booking.photographer_id == 2, "inline_explicit_actual_photographer")
                    report["checks"].append("inline_new_guest_real_photographer_no_fabricated_full_upload")
                actual_hotel = await session.get(Hotel, actual_booking.hotel_id)
                actual_photographer = await session.get(User, actual_booking.photographer_id) if actual_booking.photographer_id else None
                months = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")
                for row, raw in zip(linked, selected):
                    private_path(row.disk_path)
                    parts = row.disk_path[len(prefix) + 1:].split("/")
                    require(len(parts) == 9 and parts[0] == "delivery", "storage_hierarchy_depth")
                    require(parts[1] == actual_hotel.name.replace(" ", "_") + f"#{actual_hotel.id}", "storage_hierarchy_hotel")
                    require(parts[2] == f"{months[actual_booking.shoot_date.month - 1]}_{actual_booking.shoot_date.year}"
                            and parts[3] == f"{actual_booking.shoot_date.day:02d}", "storage_hierarchy_month_day")
                    expected_photographer = (actual_photographer.name.replace(" ", "_") + f"#{actual_photographer.id}"
                                              if actual_photographer else "Фотограф_не_указан#unknown")
                    require(parts[4] == expected_photographer and parts[5].endswith(f"_booking-{booking_id}"),
                            "storage_hierarchy_actual_photographer_and_booking")
                    require(parts[6] == "Выбранные" and parts[7] == f"Продажа-{draft_id}", "storage_hierarchy_sale_selected")
                    storage_hierarchy.add("/" + "/".join(parts[:-1]))
                    require(row.byte_size == len(raw) and row.sha256 == hashlib.sha256(raw).hexdigest(), "stored_original_identity")
                    require(await storage.download_bytes(row.disk_path) == raw, "provider_checksum_roundtrip")
            for path in (guest, guest + "/data", guest + "/album.zip", *(guest + f"/photos/{pid}" for pid in photo_ids)):
                await request("GET", path, status=404)
            report["checks"].append(f"sale_{n + 1}_pending_guest_denied")
            before = await identities()
            replay = await operation(n, "sale_complete", ref | {"price": "400.00"})
            retried = await operation(n, "sale_complete", ref | {"price": "400.00"}, suffix="-new-request-key")
            duplicate = await operation(n, "sale_selected", ref, selected[-1], suffix="-duplicate-new-request-key")
            require(replay["saleId"] == retried["saleId"] == finished["saleId"], "complete_replay_sale_identity")
            require(duplicate["photoId"] == last["photoId"] and await identities() == before,
                    "retry_no_duplicate_rows_or_commissions")

            phase = f"sale_{n + 1}_synthetic_payment"
            # There is no miniapp owner receipt-decision route. Use the same
            # receipt mutation + production payment allocator in this schema;
            # do not invoke Telegram callbacks or send any review messages.
            async with db.Session() as session:
                receipt = await session.scalar(select(Receipt).where(Receipt.booking_id == booking_id,
                                                                     Receipt.purpose == "PAYMENT"))
                require(receipt.status == "PENDING", "synthetic_receipt_pending")
                if partial_deposit and n == 0:
                    partial_sale = await session.get(Sale, finished["saleId"])
                    require(receipt.expected_amount == 400 and partial_sale.amount == 800
                            and partial_sale.payment_status == "PARTIAL", "partial_deposit_payment_due_is_400")
                    report["partial_deposit"] = {"approved_deposit": 400, "sale_amount": 800,
                                                 "payment_receipt_expected": 400, "receipt_purpose": "PAYMENT"}
                receipt.status, receipt.verified_amount = "APPROVED", receipt.expected_amount
                receipt.reviewed_by_id, receipt.reviewed_at = 1, utc_now()
                receipt.approved_image_hash = receipt.image_sha256
                receipt.approved_operation_key = receipt.operation_key
                await session.flush()
                await refresh_payment_statuses(session, booking_id)
                await session.commit()
            ready = await workflow_gallery(booking_id)
            require(ready["ready"] and ready["state"] == "ready" and ready["photoCount"] == len(selected), "paid_refresh_gallery_ready")
            require(urlsplit(ready["clientUrl"]).path == guest and ready["qrPath"] == f'/sales/{finished["saleId"]}/gallery-qr',
                    "paid_capabilities_match_sale")
            qr = await request("GET", "/api/miniapp" + ready["qrPath"], actor=3)
            require(qr.startswith(b"\x89PNG\r\n\x1a\n"), "assigned_manager_qr_png")
            await request("GET", "/api/miniapp" + ready["qrPath"], actor=4, status=403)
            replay = await operation(n, "sale_complete", ref | {"price": "400.00"})
            require(replay["saleId"] == finished["saleId"] and replay["gallery"]["ready"], "paid_replay_refreshes_gallery_ack")
            public = await request("GET", guest + "/data")
            require([p["id"] for p in public["photos"]] == photo_ids, "guest_exact_paid_photos")
            for row, raw in zip(public["photos"], selected):
                require(await request("GET", row["url"]) == raw, "guest_download_original_checksum")
            await request("GET", guest)
            if browser_mode:
                phase = f"sale_{n + 1}_browser_paid_gallery"
                await browser_driver.paid_gallery(n, selected)
            report["checks"].append(f"sale_{n + 1}_paid_guest_checksum_and_replay")
            report["checks"].append(f"sale_{n + 1}_qr_manager_allowed_unrelated_denied")
            journeys.append({"booking_id": booking_id, "draft_id": draft_id,
                "sale_id": finished["saleId"], "gallery_id": gallery.id, "photo_ids": photo_ids,
                "guest": guest})

        phase = "password_and_expiry"
        second = journeys[1]
        endpoint = f'/api/miniapp/delivery/{second["booking_id"]}'
        await request("POST", endpoint, actor=1, json={"password": "synthetic-diagnostic-password"})
        require(b'type="password"' in await request("GET", second["guest"]), "password_form")
        await request("GET", second["guest"] + "/data", status=401)
        await request("GET", second["guest"] + f'/photos/{second["photo_ids"][0]}', status=401)
        await request("POST", second["guest"] + "/unlock", status=401, data={"password": "incorrect"})
        _, unlock_headers = await request("POST", second["guest"] + "/unlock", status=303,
            data={"password": "synthetic-diagnostic-password"}, allow_redirects=False, response_meta=True)
        # The production cookie is Secure. The ephemeral test server uses HTTP,
        # so forward that returned cookie explicitly for this localhost request.
        cookie = unlock_headers["Set-Cookie"].split(";", 1)[0]
        unlocked = await request("GET", second["guest"] + "/data", extra_headers={"Cookie": cookie})
        require([p["id"] for p in unlocked["photos"]] == second["photo_ids"], "password_unlock_scoped_photos")
        await request("POST", endpoint, actor=1, json={"password": ""})
        async with db.Session() as session:
            gallery = await session.get(DeliveryGallery, second["gallery_id"])
            expires_at = gallery.expires_at
            gallery.expires_at = utc_now() - timedelta(seconds=1)
            await session.commit()
        require((await workflow_gallery(second["booking_id"]))["state"] == "expired", "expiry_workflow_truth")
        await request("GET", second["guest"] + "/data", status=404)
        await request("GET", second["guest"] + f'/photos/{second["photo_ids"][0]}', status=404)
        await request("GET", f'/api/miniapp/sales/{second["sale_id"]}/gallery-qr', actor=3, status=409)
        async with db.Session() as session:
            (await session.get(DeliveryGallery, second["gallery_id"])).expires_at = expires_at
            await session.commit()
        report["checks"].append("password_and_expiry_enforced")

        phase = "cross_sale_and_manual_close"
        first, second = journeys
        require(first["sale_id"] != second["sale_id"] and first["gallery_id"] != second["gallery_id"], "independent_sale_identity")
        if browser_mode:
            async with db.Session() as session:
                b1, b2 = [await session.get(Booking, b["booking_id"]) for b in journeys]
                c1, c2 = [await session.get(Client, b.client_id) for b in (b1, b2)]
                require(b1.id != b2.id and c1.id != c2.id and c1.name == c2.name,
                        "same_guest_names_do_not_merge_client_or_booking")
            require(browser_driver.report["inline_new_guest_sales"] == 1, "ui_inline_guest_case_exercised")
            report["checks"].append("same_guest_names_distinct_clients_and_bookings")
        await request("GET", first["guest"] + f'/photos/{second["photo_ids"][0]}', status=404)
        before = await identities()
        await request("POST", f'/api/miniapp/delivery/{first["booking_id"]}', actor=1, json={"published": False})
        closed = await operation(0, "sale_complete", {"draft": {"id": first["draft_id"]}, "price": "400.00"}, suffix="-after-close")
        require(closed["gallery"]["state"] == "closed" and not closed["gallery"]["ready"], "manual_close_preserved_on_retry")
        require((await workflow_gallery(first["booking_id"]))["state"] == "closed", "manual_close_workflow_truth")
        await request("GET", first["guest"] + "/data", status=404)
        await request("GET", second["guest"] + "/data")
        require(await identities() == before, "close_retry_preserves_all_row_identities")
        require(blocked.await_count == 0, "no_bot_delivery")
        report["checks"].extend(["two_independent_sales_no_cross_gallery_leak", "manual_close_preserved", "no_external_messages"])
        from app.operations_guard import _lease_engines
        require(engine in _lease_engines, "unpooled_lease_exercised")
        for created in engines:
            async with created.connect() as connection:
                require(await connection.scalar(text('SELECT marker FROM _pb_diag_sentinel')) == sentinel,
                        "all_engines_sentinel")
        counts = await identities()
        expected_photos = sum(len(selected) for selected in selections)
        require(len(counts["sales"]) == 2 and len(counts["receipts"]) == (3 if partial_deposit else 2)
                and len(counts["sale_draft_photos"]) == expected_photos and len(counts["delivery_photos"]) == expected_photos,
                "final_synthetic_counts")
        report.update(ok=True, http_requests=served["requests"], engines=len(engines),
            connection_schema_checks=isolation["connect_checks"],
            row_counts={key: len(value) for key, value in counts.items()},
            provider_upload_attempts=provider["uploads"], provider_downloads=provider["downloads"],
            synthetic_png_bytes=[len(raw) for raw in photos], storage_hierarchy=sorted(storage_hierarchy))
        report["checks"].append("disk_hotel_month_day_actual_photographer_booking_sale_hierarchy")
        if browser_mode:
            report["browser"] = browser_driver.report
            report["checks"].append("real_app_one_save_ui_payment_url_qr_guest_checksums")
        report["checks"].append("all_engines_and_unpooled_lease_schema_isolated")
    except Exception as exc:
        failure = exc
        report.update(ok=False, failed_phase=phase, error_type=type(exc).__name__)
        if browser_driver is not None:
            report["browser"] = browser_driver.report
        if isinstance(exc, CheckFailed):
            report["error_check"] = str(exc)
    finally:
        report["audit_ok"] = report["ok"]
        if browser_driver is not None:
            try:
                await browser_driver.close()
                report["cleanup"]["browser_closed"] = True
            except Exception:
                report["cleanup"]["browser_closed"] = False
        if client is not None:
            try:
                await client.close()
                report["cleanup"]["http_closed"] = True
            except Exception:
                report["cleanup"]["http_closed"] = False
        if storage is not None and provider_cleanup_allowed:
            try:
                report["provider_cleanup"] = await delete_diagnostic_uuid_directory(storage, prefix,
                    expected_uuid=uid, provider_error_type=yandex_disk.YandexDiskError)
                report["cleanup"]["provider_uuid_directory_removed"] = True
            except Exception as exc:
                report["cleanup"]["provider_uuid_directory_removed"] = False
                report["cleanup"]["provider_error_type"] = type(exc).__name__
        if schema_created:
            try:
                async with engines[-1].begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                    isolation["ready"] = False
                    require(not await connection.scalar(text('SELECT 1 FROM pg_namespace WHERE nspname=:name'),
                                                        {"name": schema}), "schema_cleanup_verification")
                report["cleanup"]["uuid_schema_removed"] = True
            except Exception as exc:
                report["cleanup"]["uuid_schema_removed"] = False
                report["cleanup"]["schema_error_type"] = type(exc).__name__
        for created in engines:
            await created.dispose()
        if tls_directory is not None:
            tls_directory.cleanup()
        sa_async.create_async_engine = original_create
    report["ok"] = report["ok"] and all(value is True for key, value in report["cleanup"].items() if not key.endswith("error_type"))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if report["ok"] and failure is None else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run()))
    except Exception as exc:
        # No traceback: exception messages from drivers can contain DB targets,
        # and provider exceptions can contain temporary private download URLs.
        result = {"ok": False, "error_type": type(exc).__name__}
        if isinstance(exc, CheckFailed):
            result["error_check"] = str(exc)
        print(json.dumps(result, sort_keys=True), flush=True)
        sys.exit(1)

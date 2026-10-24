"""Atomic text schedule imports, kept separate from attendance and access roles.

Daily metadata and idempotency receipts use the existing transactional settings
store. Only unstarted, non-historical PLANNED shifts may be replaced.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone

from aiohttp import web
from sqlalchemy import text

from .miniapp_security import (
    AccessError,
    parse_shift,
    require_schedule_editor,
    utc_bounds,
)
from .schedule_text import parse_schedule

DAY_PREFIX = "schedule.day:"
RECEIPT_PREFIX = "schedule.import:"


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


def positive(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,9}", str(value or "")):
        raise AccessError("Выберите сотрудника и отель из списка.", 400)
    value = int(value)
    if value >= 2**31:
        raise AccessError("Некорректный номер сотрудника или отеля.", 400)
    return value


def moment(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def day_key(uid, day):
    return f"{DAY_PREFIX}{day}:{uid}"


async def put(conn, key, value):
    await conn.execute(text("""INSERT INTO settings(key,value) VALUES (:key,:value)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value"""),
        {"key": key, "value": packed(value)})


async def forget_day_plan(conn, uid, day):
    """A later explicit manual edit supersedes the imported plan for that day."""
    await conn.execute(text("DELETE FROM settings WHERE key=:key"), {"key": day_key(uid, str(day))})


async def stored_days(api, conn, first, last):
    rows = await api.rows(conn, "SELECT key,value FROM settings WHERE key>=:lo AND key<:hi ORDER BY key",
        lo=DAY_PREFIX + str(first) + ":",
        hi=DAY_PREFIX + str(date.fromisoformat(str(last)) + timedelta(days=1)) + ":")
    return [json.loads(row["value"]) for row in rows]


async def decorate_calendar(api, conn, actor, first, last, items, *, team=False, include_days=False):
    plans = await stored_days(api, conn, first, last)
    roles = set(actor["roles"])
    allowed_hotels = set()
    if "OWNER" not in roles and ("ADMIN" in roles or team):
        allowed_hotels = {r["hotel_id"] for r in await api.rows(conn,
            "SELECT hotel_id FROM hotel_employees WHERE user_id=:uid", uid=actor["id"])}
        if "ADMIN" not in roles:
            allowed_hotels.update(x["hotelId"] for x in items if x["userId"] == actor["id"])
    names, hotels = {}, {}
    for plan in plans:
        uid, hid = plan["userId"], plan["hotelId"]
        if "OWNER" not in roles:
            if "ADMIN" in roles and hid not in allowed_hotels:
                continue
            if "ADMIN" not in roles and uid != actor["id"] and (not team or hid not in allowed_hotels):
                continue
        same_day = [x for x in items if x["userId"] == uid and x["date"] == plan["date"] and x["status"] != "CANCELLED"]
        matching = [x for x in same_day if x["hotelId"] == hid
                    and x["start"] == plan["start"] and x["end"] == plan["end"]]
        if plan["role"] != "OFF" and matching:
            for item in matching:
                item["role"], item["plannedRole"], item["importedPlan"] = plan["role"], plan["role"], True
            continue
        if not include_days:
            continue
        if uid not in names:
            rows = await api.rows(conn, "SELECT name FROM users WHERE id=:uid", uid=uid)
            names[uid] = rows[0]["name"] if rows else f"Сотрудник №{uid}"
        if hid not in hotels:
            rows = await api.rows(conn, "SELECT name FROM hotels WHERE id=:hid", hid=hid)
            hotels[hid] = rows[0]["name"] if rows else f"Отель №{hid}"
        items.append({"id": f"plan-{uid}-{plan['date']}", "userId": uid, "name": names[uid],
            "role": plan["role"], "plannedRole": plan["role"], "hotelId": hid, "hotel": hotels[hid],
            "date": plan["date"], "start": plan["start"] or "", "end": plan["end"] or "",
            "status": "DAY_OFF" if plan["role"] == "OFF" else "PLAN_ONLY",
            "attendance": "DAY_OFF" if plan["role"] == "OFF" else "PLAN_ONLY",
            "importedPlan": True, "factsPreserved": bool(same_day), "breaks": []})
    return sorted(items, key=lambda x: (x["date"], x["name"], x["start"], str(x["id"])))


class ScheduleImport:
    def __init__(self, api):
        self.api = api

    def payload(self, body, *, apply=False):
        allowed = {"userId", "text", "defaults"} | ({"revision", "requestKey"} if apply else set())
        if set(body) - allowed or not {"userId", "text"}.issubset(body):
            raise AccessError("Нужны выбранный сотрудник и текст графика.", 400)
        uid = positive(body["userId"])
        defaults = body.get("defaults", {})
        if not isinstance(defaults, dict) or set(defaults) - {"hotelId", "start", "end"}:
            raise AccessError("Проверьте общий отель и время.", 400)
        defaults = {k: v for k, v in defaults.items() if v not in ("", None)}
        if "hotelId" in defaults:
            defaults["hotelId"] = positive(defaults["hotelId"])
        for key in ("start", "end"):
            if key in defaults and (not isinstance(defaults[key], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", defaults[key])):
                raise AccessError("Время нужно указать в формате ЧЧ:ММ.", 400)
        if apply and (not re.fullmatch(r"[a-f0-9]{64}", str(body.get("revision", "")))
                      or not re.fullmatch(r"[a-zA-Z0-9_-]{16,64}", str(body.get("requestKey", "")))):
            raise AccessError("Сначала проверьте график, затем нажмите «Применить весь график».", 400)
        return uid, parse_schedule(body["text"]), defaults

    async def authorize(self, conn, actor, uid):
        require_schedule_editor(actor["roles"])
        people = {}
        suffix = " FOR NO KEY UPDATE" if conn.dialect.name == "postgresql" else ""
        for person in sorted({actor["id"], uid}):
            rows = await self.api.rows(conn, "SELECT id,name,active FROM users WHERE id=:uid" + suffix, uid=person)
            if not rows or not rows[0]["active"]:
                raise AccessError("Сотрудник или руководитель неактивен.", 403)
            people[person] = dict(rows[0])
        roles = {r["role"] for r in await self.api.rows(conn,
            "SELECT role FROM user_roles WHERE user_id=:uid", uid=actor["id"])}
        require_schedule_editor(roles)
        target_roles = {r["role"] for r in await self.api.rows(conn,
            "SELECT role FROM user_roles WHERE user_id=:uid", uid=uid)}
        if not target_roles & {"MANAGER", "PHOTOGRAPHER"}:
            raise AccessError("Выбранный сотрудник не имеет рабочей роли.", 400)
        hotels = {r["hotel_id"] for r in await self.api.rows(conn,
            "SELECT hotel_id FROM hotel_employees WHERE user_id=:uid", uid=actor["id"])}
        return roles, hotels, people[uid]["name"], target_roles

    @staticmethod
    def scope(roles, hotels, ids):
        if "OWNER" not in roles and any(h not in hotels for h in ids):
            raise AccessError("Изменять график можно только в назначенных вам отелях. В периоде есть чужой отель.", 403)

    async def prepare(self, conn, actor, uid, parsed, defaults, grants):
        roles, allowed, name, target_roles = grants
        first, last = date.fromisoformat(parsed["from"]), date.fromisoformat(parsed["to"])
        lower, upper = utc_bounds(first, last, self.api.tz)
        shifts = [dict(r) for r in await self.api.rows(conn, """SELECT * FROM shifts
            WHERE user_id=:uid AND start_at<:hi AND end_at>:lo AND status<>'CANCELLED'
            ORDER BY start_at,id""", uid=uid, lo=lower, hi=upper)]
        prior = [dict(r) for r in await self.api.rows(conn, """SELECT * FROM shifts
            WHERE user_id=:uid AND start_at>=:lo AND start_at<:hi AND status<>'CANCELLED'
            ORDER BY start_at,id""", uid=uid, lo=lower-timedelta(days=31), hi=lower)]
        existing = [p for p in await stored_days(self.api, conn, first, last) if p["userId"] == uid]
        self.scope(roles, allowed, {s["hotel_id"] for s in shifts} | {p["hotelId"] for p in existing})
        assigned = await self.api.rows(conn, """SELECT DISTINCT h.id FROM hotels h
            JOIN hotel_employees he ON he.hotel_id=h.id
            WHERE he.user_id=:uid AND h.active=TRUE ORDER BY h.id""", uid=uid)
        confirmations = {}
        protected = {str(first+timedelta(days=i)) for i in range((last-first).days+1)
                     if first+timedelta(days=i) < self.api.today()}
        for table, field in (("shift_check_ins", "started_at"), ("shift_check_outs", "ended_at"), ("work_breaks", "started_at")):
            confirmations[table] = [dict(r) for r in await self.api.rows(conn,
                f"SELECT * FROM {table} WHERE user_id=:uid AND shift_date>=:lo AND shift_date<=:hi ORDER BY id",
                uid=uid, lo=first, hi=last)]
            for row in confirmations[table]:
                if row.get(field) or row.get("offline_claimed_at") or row.get("status") == "PENDING_REVIEW":
                    protected.add(str(row["shift_date"])[:10])
        by_day = {}
        configs = set()
        for shift in shifts + prior:
            st, en = moment(shift["start_at"]).astimezone(self.api.tz), moment(shift["end_at"]).astimezone(self.api.tz)
            if st.date() == en.date():
                configs.add((shift["hotel_id"], st.strftime("%H:%M"), en.strftime("%H:%M")))
            if shift in shifts:
                if not first <= st.date() <= last or en.date() != st.date():
                    raise AccessError("Смена пересекает границу дня или периода. Сначала уточните её отдельно; вне указанного периода ничего не изменено.", 409)
                by_day.setdefault(str(st.date()), []).append(shift)
                if shift["status"] != "PLANNED":
                    protected.add(str(st.date()))
        shared = {}
        if len(configs) == 1:
            hid, start, end = next(iter(configs))
            shared = {"hotelId": hid, "start": start, "end": end}
        else:
            if len({x[0] for x in configs}) == 1:
                shared["hotelId"] = next(iter(configs))[0]
            if len({(x[1], x[2]) for x in configs}) == 1:
                _, shared["start"], shared["end"] = next(iter(configs))
        if "hotelId" not in shared and len(assigned) == 1:
            shared["hotelId"] = assigned[0]["id"]
        known = {p["date"]: p for p in existing}
        days, needs = [], set()
        for entry in parsed["days"]:
            current = by_day.get(entry["date"], [])
            config = dict(shared)
            if entry["date"] in known:
                config.update({k: known[entry["date"]][k] for k in ("hotelId", "start", "end")
                               if known[entry["date"]][k] is not None})
            elif len(current) == 1:
                config.update(hotelId=current[0]["hotel_id"],
                    start=moment(current[0]["start_at"]).astimezone(self.api.tz).strftime("%H:%M"),
                    end=moment(current[0]["end_at"]).astimezone(self.api.tz).strftime("%H:%M"))
            config.update(defaults)
            required = ("hotelId",) if entry["role"] == "OFF" else ("hotelId", "start", "end")
            needs.update(k for k in required if not config.get(k))
            days.append({"userId": uid, **entry, "hotelId": config.get("hotelId"),
                         "start": config.get("start") if entry["role"] != "OFF" else None,
                         "end": config.get("end") if entry["role"] != "OFF" else None})
        # Do not introduce or cancel an already elapsed shift today: doing so
        # could retroactively affect the attendance/fine worker. Keep a plan only.
        now = datetime.now(timezone.utc).astimezone(self.api.tz)
        if now.date() == self.api.today():
            for entry in days:
                if entry["date"] == str(self.api.today()):
                    started_by_clock = entry["start"] and entry["start"] <= now.strftime("%H:%M")
                    if started_by_clock or any(moment(s["start_at"]) <= now for s in by_day.get(entry["date"], [])):
                        protected.add(entry["date"])
        selected_hotels = {d["hotelId"] for d in days if d["hotelId"] is not None}
        self.scope(roles, allowed, selected_hotels)
        hotel_names = {}
        for hid in selected_hotels:
            rows = await self.api.rows(conn, "SELECT name FROM hotels WHERE id=:hid AND active=TRUE", hid=hid)
            if not rows:
                raise AccessError("В графике указан недоступный отель.", 400)
            hotel_names[hid] = rows[0]["name"]
        if not needs:
            for entry in days:
                if entry["role"] != "OFF":
                    parse_shift({k: entry[k] for k in ("userId", "hotelId", "date", "start", "end")},
                                self.api.tz, min(first, self.api.today()))
        warnings = []
        if protected:
            warnings.append("Прошедшие и начавшиеся смены сохраняются без изменения. Для этих дат обновится только план, не факт работы.")
        if any(d["role"] not in target_roles for d in days if d["role"] != "OFF"):
            warnings.append("Назначение роли на день не добавляет сотруднику новых прав доступа.")
        revision = digest({"days": days, "old": existing, "shifts": shifts,
            "confirmations": confirmations, "today": self.api.today(), "protected": sorted(protected),
            "roles": sorted(roles), "allowed": sorted(allowed)})
        public = {**parsed, "days": [{**d, "hotel": hotel_names.get(d["hotelId"], ""),
                    "factsPreserved": d["date"] in protected} for d in days],
                  "employee": {"id": uid, "name": name}, "needs": sorted(needs),
                  "warnings": warnings, "revision": revision, "canApply": not needs}
        return public, days, by_day, protected, existing

    async def preview(self, request):
        actor = request["miniapp_actor"]
        body = await self.api.body(request, max_bytes=65536)
        uid, parsed, defaults = self.payload(body)
        async with self.api.engine.begin() as conn:
            grants = await self.authorize(conn, actor, uid)
            result, *_ = await self.prepare(conn, actor, uid, parsed, defaults, grants)
        return web.json_response(result)

    async def verify_receipt(self, actor, key):
        async with self.api.engine.begin() as conn:
            rows = await self.api.rows(conn, "SELECT value FROM settings WHERE key=:key", key=key)
            if not rows:
                raise AccessError("Сохранение ещё не подтверждено сервером.", 409)
            receipt = json.loads(rows[0]["value"])
            grants = await self.authorize(conn, actor, receipt["userId"])
            self.scope(grants[0], grants[1], {d["hotelId"] for d in receipt["days"]})
            current = [p for p in await stored_days(self.api, conn, receipt["from"], receipt["to"])
                       if p["userId"] == receipt["userId"]]
            if sorted(current, key=lambda p: p["date"]) != receipt["days"]:
                raise AccessError("После сохранения график был изменён. Обновите предварительную проверку.", 409)
            for expected in receipt["shiftChecks"]:
                lo, hi = utc_bounds(date.fromisoformat(expected["date"]), date.fromisoformat(expected["date"]), self.api.tz)
                actual = await self.api.rows(conn, """SELECT id,hotel_id,start_at,end_at FROM shifts
                    WHERE user_id=:uid AND start_at>=:lo AND start_at<:hi AND status<>'CANCELLED' ORDER BY id""",
                    uid=receipt["userId"], lo=lo, hi=hi)
                normalized = [self.shift_signature(r) for r in actual]
                if normalized != expected["shifts"]:
                    raise AccessError("План смен изменён после сохранения. Проверьте график заново.", 409)
            return {k: receipt[k] for k in ("userId", "from", "to", "counts", "days")} | {
                "verified": True, "changedShifts": receipt["changedShifts"],
                "preservedDays": receipt["preservedDays"]}

    @staticmethod
    def shift_signature(row):
        return {"id": row["id"], "hotelId": row["hotel_id"],
                "start": moment(row["start_at"]).isoformat(), "end": moment(row["end_at"]).isoformat()}

    async def apply_body(self, actor, body):
        uid, parsed, defaults = self.payload(body, apply=True)
        key = f"{RECEIPT_PREFIX}{actor['id']}:{body['requestKey']}"
        fingerprint = digest({"userId": uid, "text": body["text"], "defaults": defaults})
        async with self.api.engine.begin() as conn:
            grants = await self.authorize(conn, actor, uid)
            old = await self.api.rows(conn, "SELECT value FROM settings WHERE key=:key", key=key)
            if old:
                if json.loads(old[0]["value"])["fingerprint"] != fingerprint:
                    raise AccessError("Этот запрос уже использован для другого графика.", 409)
            else:
                result, days, by_day, protected, existing = await self.prepare(conn, actor, uid, parsed, defaults, grants)
                if not result["canApply"]:
                    raise AccessError("Укажите недостающие отель и часы один раз для всего графика, затем проверьте его.", 400)
                if result["revision"] != body["revision"]:
                    raise AccessError("График, права или фактическая смена изменились после проверки. Проверьте текст заново.", 409)
                changed, checks = 0, []
                previous = {d["date"]: d for d in existing}
                for entry in days:
                    if previous.get(entry["date"]) != entry:
                        await put(conn, day_key(uid, entry["date"]), entry)
                    if entry["date"] in protected:
                        continue
                    current = by_day.get(entry["date"], [])
                    intervals = None
                    if entry["role"] != "OFF":
                        intervals = parse_shift({k: entry[k] for k in ("userId", "hotelId", "date", "start", "end")},
                                                self.api.tz, self.api.today())
                    identical = not current if intervals is None else (
                        len(current) == 1 and current[0]["hotel_id"] == intervals[1]
                        and moment(current[0]["start_at"]).replace(tzinfo=None) == intervals[2]
                        and moment(current[0]["end_at"]).replace(tzinfo=None) == intervals[3])
                    if not identical:
                        for shift in current:
                            await conn.execute(text("UPDATE shifts SET status='CANCELLED' WHERE id=:id AND status='PLANNED'"),
                                               {"id": shift["id"]})
                            changed += 1
                        current = []
                        if intervals is not None:
                            _, hid, start, end = intervals
                            current = [dict(r) for r in await self.api.rows(conn, """INSERT INTO shifts
                                (user_id,hotel_id,start_at,end_at,status) VALUES (:uid,:hid,:start,:end,'PLANNED')
                                RETURNING id,hotel_id,start_at,end_at""", uid=uid, hid=hid, start=start, end=end)]
                            changed += 1
                    checks.append({"date": entry["date"], "shifts": [self.shift_signature(r) for r in current]})
                receipt = {"userId": uid, "fingerprint": fingerprint, "from": parsed["from"], "to": parsed["to"],
                    "counts": parsed["counts"], "days": days, "shiftChecks": checks,
                    "changedShifts": changed, "preservedDays": sorted(protected)}
                await put(conn, key, receipt)
                await self.api.audit_write(conn, actor, "miniapp_schedule_text_applied", "user", uid,
                    f"{result['employee']['name']} · {parsed['from']}–{parsed['to']} · "
                    f"менеджер {parsed['counts']['MANAGER']}, фотограф {parsed['counts']['PHOTOGRAPHER']}, "
                    f"выходные {parsed['counts']['OFF']}; факт работы и права сохранены")
        # A new connection reads committed data, never the in-memory preview.
        return await self.verify_receipt(actor, key)

    async def apply(self, request):
        body = await self.api.body(request, max_bytes=65536)
        return web.json_response(await self.apply_body(request["miniapp_actor"], body))

    async def verify(self, request):
        body = await self.api.body(request)
        if set(body) != {"requestKey"} or not re.fullmatch(r"[a-zA-Z0-9_-]{16,64}", str(body["requestKey"])):
            raise AccessError("Некорректный номер сохранения.", 400)
        actor = request["miniapp_actor"]
        return web.json_response(await self.verify_receipt(actor, f"{RECEIPT_PREFIX}{actor['id']}:{body['requestKey']}"))


def install_schedule_import(app, api):
    service = ScheduleImport(api)
    app["schedule_import"] = service
    app.router.add_post("/api/miniapp/schedule/text/preview", service.preview)
    app.router.add_post("/api/miniapp/schedule/text/apply", service.apply)
    app.router.add_post("/api/miniapp/schedule/text/verify", service.verify)
    return service

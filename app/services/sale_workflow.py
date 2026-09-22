"""Sale workflow helpers: photographer percentage is final only after full-shoot upload."""
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import func, or_, select

from ..models import (
    Booking,
    Package,
    PayrollEntry,
    Photo,
    Receipt,
    Sale,
    SaleDraft,
    SaleDraftPhoto,
    Shooting,
    User,
)
from .commissions import (
    MANAGER_PERCENT,
    booking_photo_price,
    photographer_bonus,
    photographer_percent,
)
from .core import audit
from .receipts import money, operation_key, payment_totals, refresh_payment_statuses


async def full_shoot_state(session, booking_id: int):
    shooting = await session.scalar(
        select(Shooting).where(Shooting.booking_id == booking_id)
    )
    count = 0
    if shooting is not None:
        count = int(
            await session.scalar(
                select(func.count(Photo.id)).where(Photo.shooting_id == shooting.id)
            )
            or 0
        )
    return shooting, count


async def final_photographer_percent(session, booking_id: int):
    shooting, count = await full_shoot_state(session, booking_id)
    if shooting is None or shooting.full_upload_completed_at is None:
        return None, count
    return photographer_percent(count), count


async def finalize_photographer_commissions(session, booking: Booking):
    percent, count = await final_photographer_percent(session, booking.id)
    if percent is None or not booking.photographer_id:
        return {"finalized": False, "count": count, "percent": None, "sales": 0}
    rows = (
        await session.scalars(
            select(Sale).where(
                Sale.booking_id == booking.id,
                Sale.credited_user_id == booking.photographer_id,
                Sale.commission_role == "PHOTOGRAPHER",
            )
        )
    ).all()
    now = datetime.now(UTC).replace(tzinfo=None)
    for sale in rows:
        amount = Decimal(str(sale.amount))
        commission = (amount * percent / 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        sale.percent = float(percent)
        sale.commission = float(commission)
        sale.commission_finalized_at = now
    return {
        "finalized": True,
        "count": count,
        "percent": percent,
        "sales": len(rows),
    }


async def complete_full_upload(session, actor, shooting_id, *, allow_management=False):
    from types import SimpleNamespace

    from .core import roles_of
    from .photo_storage import storage_summary

    shooting = await session.get(Shooting, shooting_id, with_for_update=True)
    booking = await session.get(Booking, shooting.booking_id) if shooting else None
    roles = await roles_of(session, actor)
    management = allow_management and roles & {'OWNER', 'ADMIN'}
    if booking is None or (booking.photographer_id != actor.id and not management):
        raise ValueError('Нет доступа к съёмке.')
    if shooting.status != 'READY_FOR_SALE' or shooting.full_upload_completed_at is not None:
        raise ValueError('Загрузка уже закрыта.')
    count = await session.scalar(select(func.count(Photo.id)).where(Photo.shooting_id == shooting.id))
    declared = await session.scalar(select(func.max(Sale.declared_photo_count)).where(Sale.booking_id == booking.id))
    if not count:
        raise ValueError('Сначала загрузите всю съёмку.')
    if declared and count < declared:
        raise ValueError(f'По продаже указано {declared} кадров, а загружено {count}. Загрузите оставшиеся кадры перед завершением.')
    sync = await storage_summary(session, shooting.id)
    shooting.full_upload_completed_at = datetime.now(UTC).replace(tzinfo=None)
    commissions = await finalize_photographer_commissions(session, booking)
    await audit(session, actor, 'full_shoot_upload_completed', 'shooting', shooting.id,
                f"photos={count};percent={commissions['percent']};sales_updated={commissions['sales']};disk_pending={sync['pending']};disk_failed={sync['failed']}")
    return SimpleNamespace(shooting=shooting, booking=booking, count=count, sync=sync, commissions=commissions)


ACTIVE_DRAFT_STATUSES = ("AWAITING_RECEIPT", "AWAITING_COUNTS", "AWAITING_SELECTED")

async def can_sell(actor, roles, booking):
    if booking is None or booking.status != "READY_FOR_SALE":
        return False
    if roles & {"OWNER", "ADMIN"}:
        return True
    if "MANAGER" in roles and booking.manager_id == actor.id:
        return True
    return "PHOTOGRAPHER" in roles and booking.photographer_id == actor.id


async def active_draft(session, booking_id):
    return await session.scalar(
        select(SaleDraft)
        .where(
            SaleDraft.booking_id == booking_id,
            SaleDraft.status.in_(ACTIVE_DRAFT_STATUSES),
        )
        .order_by(SaleDraft.id.desc())
        .limit(1)
    )


async def selected_count(session, draft_id):
    return int(
        await session.scalar(
            select(func.count(SaleDraftPhoto.id)).where(
                SaleDraftPhoto.draft_id == draft_id
            )
        )
        or 0
    )


async def start_sale_draft(session, actor, roles, booking_id):
    booking = await session.get(Booking, booking_id, with_for_update=True)
    if not await can_sell(actor, roles, booking):
        raise ValueError('Эта запись ещё не готова к продаже или недоступна.')
    draft = await active_draft(session, booking.id)
    if draft is not None and draft.created_by_id != actor.id:
        raise ValueError('Эту продажу уже оформляет другой сотрудник.')
    if draft is None:
        draft = SaleDraft(booking_id=booking.id, created_by_id=actor.id, status='AWAITING_RECEIPT')
        session.add(draft)
        await session.flush()
        await audit(session, actor, 'sale_draft_started', 'sale_draft', draft.id, f'booking={booking.id}')
    return draft


async def capture_draft_receipt(session, actor, draft, file_id, unique_id, digest, analysis):
    import json
    if draft.created_by_id != actor.id or draft.status != 'AWAITING_RECEIPT':
        raise ValueError('Этот черновик продажи уже закрыт.')
    op_key = operation_key(analysis.get('fields', {}))
    existing = await session.scalar(select(Receipt.id).where(or_(
        Receipt.file_unique_id == unique_id, Receipt.image_sha256 == digest,
        Receipt.operation_key == op_key if op_key else False)).limit(1))
    duplicate = await session.scalar(select(SaleDraft.id).where(SaleDraft.id != draft.id, or_(
        SaleDraft.receipt_file_unique_id == unique_id, SaleDraft.receipt_image_sha256 == digest,
        SaleDraft.receipt_operation_key == op_key if op_key else False)).limit(1))
    if existing or duplicate:
        raise ValueError('Этот чек уже использовался или похож на ранее загруженный.')
    draft.receipt_file_id = file_id
    draft.receipt_file_unique_id = unique_id
    draft.receipt_image_sha256 = digest
    draft.receipt_operation_key = op_key
    draft.receipt_analysis = json.dumps(analysis, ensure_ascii=False)
    draft.status = 'AWAITING_COUNTS'
    await audit(session, actor, 'sale_receipt_captured', 'sale_draft', draft.id)


async def set_sale_counts(session, actor, draft, total, sold, discount=0):
    if draft.created_by_id != actor.id or draft.status != 'AWAITING_COUNTS':
        raise ValueError('Черновик продажи уже закрыт.')
    if type(total) is not int or type(sold) is not int or not 0 < sold <= total <= 10000:
        raise ValueError('Проверьте общее и проданное количество кадров.')
    booking = await session.get(Booking, draft.booking_id)
    package = await session.get(Package, booking.package_id)
    if package is None:
        raise ValueError('Пакет недоступен.')
    already = await session.scalar(select(func.coalesce(func.sum(Sale.sold_photos), 0)).where(Sale.booking_id == booking.id))
    if already + sold > total:
        raise ValueError(f'Уже продано: {already}. Итог превышает общее количество кадров.')
    price = await booking_photo_price(session, booking)
    if not price.is_finite() or price <= 0:
        raise ValueError('Некорректная цена пакета.')
    from .core import roles_of
    try:
        discount = Decimal(str(discount))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('Некорректная скидка.') from exc
    if not discount.is_finite() or not 0 <= discount <= 50 or discount != money(discount):
        raise ValueError('Скидка должна быть от 0 до 50%.')
    if discount and (sold != total or already):
        raise ValueError('Скидка доступна только на всю съёмку, без предыдущих продаж.')
    if discount and (booking.photographer_id != actor.id or 'PHOTOGRAPHER' not in await roles_of(session, actor)):
        raise ValueError('Скидку назначает фотограф этой съёмки.')
    draft.unit_price = price
    draft.discount_percent = discount
    amount = money(Decimal(sold) * price * (100 - discount) / 100)
    before, paid, _ = await payment_totals(session, booking.id)
    expected = max(money(before) + amount - money(paid), Decimal(0))
    draft.declared_photo_count = total
    draft.sold_photos = sold
    draft.expected_amount = expected or None
    draft.status = 'AWAITING_SELECTED'
    return amount


async def complete_sale(session, actor, draft_id):
    """Commit-free canonical sale operation, shared by bot and offline replay."""
    from types import SimpleNamespace

    from .core import roles_of
    roles = await roles_of(session, actor)
    preview = await session.get(SaleDraft, draft_id)
    if preview is None:
        raise ValueError("Продажа не найдена.")
    booking = await session.get(Booking, preview.booking_id, with_for_update=True)
    draft = await session.get(SaleDraft, draft_id, with_for_update=True, populate_existing=True)
    if draft.created_by_id != actor.id or not await can_sell(actor, roles, booking):
        raise ValueError("Нет доступа к этой продаже.")
    if draft.status != "AWAITING_SELECTED":
        raise ValueError("Продажа уже завершена или отменена.")
    count = await selected_count(session, draft.id)
    if not draft.sold_photos or count < draft.sold_photos:
        raise ValueError(f"Сначала загрузите все выбранные фотографии: {count}/{draft.sold_photos or 0}.")
    already_sold = await session.scalar(select(func.coalesce(func.sum(Sale.sold_photos), 0)).where(Sale.booking_id == booking.id))
    if not draft.declared_photo_count or already_sold + draft.sold_photos > draft.declared_photo_count:
        raise ValueError("Количество проданных кадров изменилось. Проверьте продажу.")
    photographer = await session.get(User, booking.photographer_id) if booking.photographer_id else None
    manager = await session.get(User, booking.manager_id)
    package = await session.get(Package, booking.package_id)
    if photographer is None or package is None:
        raise ValueError("Проверьте фотографа и пакет записи.")
    price = draft.unit_price or await booking_photo_price(session, booking)
    discount = draft.discount_percent or Decimal(0)
    if discount and (draft.sold_photos != draft.declared_photo_count or already_sold):
        raise ValueError("Скидка доступна только на всю съёмку.")
    sale_amount = money(Decimal(draft.sold_photos) * price * (100 - discount) / 100)
    before, paid, _ = await payment_totals(session, booking.id)
    amount = max(money(before) + sale_amount - money(paid), Decimal(0))
    percent, actual_full_count = await final_photographer_percent(
        session, booking.id
    )
    if percent is None:
        photographer_percent_value = Decimal(0)
        photographer_commission = Decimal(0)
        finalized_at = None
    else:
        photographer_percent_value = Decimal(str(percent))
        photographer_commission = (
            sale_amount * photographer_percent_value / 100
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        finalized_at = datetime.now(UTC).replace(tzinfo=None)
    sale = Sale(
        booking_id=booking.id,
        created_by_id=actor.id,
        credited_user_id=photographer.id,
        commission_role="PHOTOGRAPHER",
        sold_photos=draft.sold_photos,
        declared_photo_count=draft.declared_photo_count,
        source_draft_id=draft.id,
        amount=float(sale_amount),
        unit_price=price,
        discount_percent=discount,
        percent=float(photographer_percent_value),
        commission=float(photographer_commission),
        commission_finalized_at=finalized_at,
    )
    session.add(sale)
    await session.flush()

    receipt = None
    if draft.receipt_file_id and amount > 0:
        receipt = Receipt(
            booking_id=booking.id,
            uploaded_by_id=actor.id,
            purpose="PAYMENT",
            file_id=draft.receipt_file_id,
            file_unique_id=draft.receipt_file_unique_id,
            image_sha256=draft.receipt_image_sha256,
            operation_key=draft.receipt_operation_key,
            expected_amount=amount,
            analysis=draft.receipt_analysis,
            status="PENDING",
        )
        session.add(receipt)
        await session.flush()

    if manager is not None:
        manager_percent_value = MANAGER_PERCENT
        manager_amount = (
            sale_amount * manager_percent_value / 100
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        manager_entry = PayrollEntry(
                user_id=manager.id,
                kind="Комиссия менеджера",
                amount=float(manager_amount),
                period=datetime.now(UTC).date().isoformat(),
                note=f"sale={sale.id};booking={booking.id};base={sale_amount};percent={manager_percent_value}",
            )
        session.add(manager_entry)
        await session.flush()
        sale.manager_percent_applied = str(manager_percent_value)
        sale.manager_payroll_entry_id = manager_entry.id

    bonus = photographer_bonus(sale_amount)
    if bonus:
        session.add(PayrollEntry(user_id=photographer.id, kind="Бонус фотографа",
            amount=float(bonus), period=datetime.now(UTC).date().isoformat(),
            note=f"sale={sale.id};booking={booking.id};base={sale_amount};bonus={bonus}"))
    draft.status = "COMPLETED"
    draft.completed_at = datetime.now(UTC).replace(tzinfo=None)
    await audit(
        session,
        actor,
        "sale_created",
        "sale",
        sale.id,
        (
            f"booking={booking.id};sold={draft.sold_photos};"
            f"declared={draft.declared_photo_count};selected={count};"
            f"full_uploaded={actual_full_count};"
            f"photographer_percent={percent if percent is not None else 'PENDING'}"
        ),
    )
    await refresh_payment_statuses(session, booking.id)
    await session.flush()
    return SimpleNamespace(sale=sale, draft=draft, booking=booking, photographer=photographer,
                           receipt=receipt, count=count, percent=percent,
                           actual_full_count=actual_full_count, sale_amount=sale_amount)

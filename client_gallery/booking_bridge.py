"""Read-only bridge from client gallery module to Photo Boss production shooting data.

Uses the established DeliveryGallery -> Booking -> Shooting and DeliveryPhoto tables.
No copies of photos or separate gallery database. Never returns disk paths or
access tokens. Access must be checked by caller's authenticated role/session.
"""
from sqlalchemy import select
from app.models import Booking, DeliveryGallery, DeliveryPhoto, Shooting


def shooting_gallery_snapshot(session, *, booking_id: int):
    """Return minimal gallery data for an existing booking, without secrets.

    This helper deliberately does not authenticate users or publish a gallery.
    The Mini App API must first authorize its caller for this booking.
    """
    if type(booking_id) is not int or booking_id < 1:
        raise ValueError("booking_id must be a positive integer")
    row = session.execute(
        select(Booking.id, Shooting.id, DeliveryGallery.id,
               DeliveryGallery.title, DeliveryGallery.published)
        .outerjoin(Shooting, Shooting.booking_id == Booking.id)
        .outerjoin(DeliveryGallery, DeliveryGallery.booking_id == Booking.id)
        .where(Booking.id == booking_id)
    ).one_or_none()
    if row is None:
        return None
    bid, shooting_id, gallery_id, title, published = row
    photos = []
    if gallery_id is not None:
        photos = [
            {"id": p.id, "name": p.filename, "selected": bool(p.selected)}
            for p in session.scalars(
                select(DeliveryPhoto)
                .where(DeliveryPhoto.gallery_id == gallery_id)
                .order_by(DeliveryPhoto.id)
            )
        ]
    return {
        "booking_id": bid,
        "shooting_id": shooting_id,
        "gallery_id": gallery_id,
        "title": title,
        "published": bool(published),
        "photos": photos,
    }

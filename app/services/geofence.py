"""Hotel attendance points, with explicit handling of uncertain device GPS."""

import math
from collections.abc import Mapping

MIN_RADIUS_M = 10
MAX_RADIUS_M = 5000
EARTH_RADIUS_M = 6371008.8


def _number(value, minimum, maximum, message):
    if type(value) not in (int, float):
        raise ValueError(message)
    try:
        value = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(message) from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(message)
    return value


def validate_geofence(latitude, longitude, radius_m):
    """All-null disables the hotel radius; partial configurations are invalid."""
    if latitude is None and longitude is None and radius_m is None:
        return None, None, None
    latitude = _number(latitude, -90, 90, "Широта точки должна быть от −90 до 90.")
    longitude = _number(longitude, -180, 180, "Долгота точки должна быть от −180 до 180.")
    radius_m = _number(radius_m, MIN_RADIUS_M, MAX_RADIUS_M,
                       "Радиус точки должен быть от 10 до 5000 метров.")
    return latitude, longitude, radius_m


def distance_m(latitude, longitude, other_latitude, other_longitude):
    """Great-circle distance; clamp rounding at antipodal and identical points."""
    lat1, lon1, lat2, lon2 = map(math.radians,
                               (latitude, longitude, other_latitude, other_longitude))
    hav = (math.sin((lat2 - lat1) / 2) ** 2
           + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(max(0, min(1, hav))))


def check_hotel_geofence(hotel, latitude, longitude, accuracy):
    """Return ACCEPTED/OUTSIDE/RETRY, never treat poor GPS as absence.

    A location only passes a configured radius if its reported accuracy circle
    fits inside it. A circle overlapping the boundary must be measured again.
    Device GPS remains advisory and does not establish identity or prove presence.
    """
    latitude = _number(latitude, -90, 90, "Некорректная широта геолокации.")
    longitude = _number(longitude, -180, 180, "Некорректная долгота геолокации.")
    if accuracy is not None:
        accuracy = _number(accuracy, 0, float("inf"), "Некорректная точность геолокации.")
    get = hotel.get if isinstance(hotel, Mapping) else lambda key: getattr(hotel, key, None)
    point_lat, point_lon, radius = validate_geofence(
        get("latitude"), get("longitude"), get("attendance_radius_m"))
    if point_lat is None:
        return {"policy": "coordinates_only", "status": "UNCONFIGURED", "accepted": True,
                "distance_m": None, "radius_m": None,
                "message": "Координаты получены. Точка отеля ещё не настроена, расстояние не проверялось."}

    distance = distance_m(point_lat, point_lon, latitude, longitude)
    result = {"policy": "hotel_radius", "accepted": False,
              "distance_m": distance, "radius_m": radius}
    if accuracy is None or accuracy > radius:
        return result | {"status": "RETRY", "message":
            "Точность геолокации недостаточна. Включите точную геопозицию, выйдите на открытое место и повторите. За ошибку геолокации штраф не начисляется."}
    if distance + accuracy <= radius:
        return result | {"status": "ACCEPTED", "accepted": True,
                         "message": "Геолокация соответствует настроенной точке отеля."}
    if distance - accuracy > radius:
        return result | {"status": "OUTSIDE", "message":
            f"Вы вне радиуса точки отеля: около {distance:.0f} м, разрешено {radius:.0f} м. Подойдите к точке и повторите. За ошибку геолокации штраф не начисляется."}
    return result | {"status": "RETRY", "message":
        "Из-за погрешности GPS нельзя проверить границу точки. Подойдите ближе к точке отеля и повторите. За ошибку геолокации штраф не начисляется."}

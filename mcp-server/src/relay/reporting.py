"""Validación y conversión de ventanas de fecha para informes y métricas."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_window(query, *, default_days: int, days_min: int, days_max: int,
                 range_max_days: int = 366,
                 needs_previous: bool = False) -> dict:
    """Valida from/to, tz y fallback days. Devuelve error como string."""
    tz_name = (query.get("tz") or "UTC").strip()
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return {"error": "tz debe ser una zona IANA válida"}

    from_raw = (query.get("from") or "").strip()
    to_raw = (query.get("to") or "").strip()
    if from_raw or to_raw:
        if not from_raw or not to_raw:
            return {"error": "from y to deben venir juntos"}
        try:
            from_date = date.fromisoformat(from_raw)
            to_date = date.fromisoformat(to_raw)
            if from_date.isoformat() != from_raw or to_date.isoformat() != to_raw:
                raise ValueError
        except ValueError:
            return {"error": "from/to deben ser YYYY-MM-DD"}
        if to_date < from_date:
            return {"error": "to no puede ser anterior a from"}
        span = (to_date - from_date).days + 1
        if span > range_max_days:
            return {"error": f"rango máximo {range_max_days} días"}
        try:
            utc_window(from_raw, to_raw, tz_name)
            if needs_previous:
                previous_window(from_raw, span, tz_name)
        except (OverflowError, ValueError):
            return {"error": "rango de fechas fuera de límites"}
        return {"from_date": from_raw, "to_date": to_raw,
                "days": None, "tz": tz_name}

    try:
        days = int((query.get("days") or "").strip())
    except ValueError:
        days = default_days
    return {"from_date": "", "to_date": "",
            "days": min(max(days, days_min), days_max), "tz": tz_name}


def utc_window(from_date: str, to_date: str, tz_name: str) -> tuple[str, str, int]:
    """Convierte días locales inclusivos a instantes UTC [inicio, fin)."""
    zone = ZoneInfo(tz_name)
    start_date = date.fromisoformat(from_date)
    end_date = date.fromisoformat(to_date) + timedelta(days=1)
    start = datetime.combine(start_date, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(end_date, time.min, zone).astimezone(timezone.utc)
    return (_format_utc(start), _format_utc(end),
            (end_date - start_date).days)


def previous_window(from_date: str, span_days: int,
                    tz_name: str) -> tuple[str, str]:
    current_start = date.fromisoformat(from_date)
    previous_start = current_start - timedelta(days=span_days)
    since, until, _ = utc_window(previous_start.isoformat(),
                                 (current_start - timedelta(days=1)).isoformat(),
                                 tz_name)
    return since, until


def local_timestamp(value: str, tz_name: str) -> str:
    """SQLite scalar: started_at UTC → timestamp in requested local zone."""
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    local = stamp.astimezone(ZoneInfo(tz_name))
    return f"{local.year:04d}-{local:%m-%dT%H:%M:%S}"


def sqlite_utc_timestamp(value: str) -> str:
    """Same UTC bound in SQLite's legacy `YYYY-MM-DD HH:MM:SS` form."""
    return value.removesuffix("Z").replace("T", " ")


def _format_utc(value: datetime) -> str:
    return f"{value.year:04d}-{value:%m-%dT%H:%M:%SZ}"

"""Existing 30 calendar-day rule, evaluated consistently in the service timezone."""
from datetime import datetime, date
from zoneinfo import ZoneInfo
from config import SERVICE_TIMEZONE


def service_today():
    return datetime.now(ZoneInfo(SERVICE_TIMEZONE)).date()


def active_access(user, today=None):
    if not user or not user.get('paid'): return False
    try:
        elapsed=((today or service_today())-date.fromisoformat(user['purchase_date'])).days
        return 0 <= elapsed < 30
    except (KeyError, TypeError, ValueError): return False

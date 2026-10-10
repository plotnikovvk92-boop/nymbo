import uuid
import re
from urllib.parse import urlsplit
from decimal import Decimal
from aiohttp import BasicAuth, ClientSession, ClientTimeout

from config import (
    YOOKASSA_API_URL, YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY,
    PRICE_KOPECKS, CURRENCY, PUBLIC_OFFER_VERSION, TEST_MODE,
)


def _rub(amount_kopecks: int) -> str:
    return f"{Decimal(amount_kopecks) / Decimal(100):.2f}"


def _auth() -> BasicAuth:
    return BasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)


def payment_payload(user_id: int, return_url: str, idempotence_key: str) -> dict:
    if not return_url.startswith("https://"):
        raise ValueError("Return URL requires HTTPS")
    return {
        "amount": {"value": _rub(PRICE_KOPECKS), "currency": CURRENCY},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": "Информационный план питания на 30 дней",
        "metadata": {
            "user_id": str(user_id),
            "offer_version": PUBLIC_OFFER_VERSION,
            "order_key": idempotence_key,
        },
    }


async def create_yookassa_payment(user_id: int, return_url: str, idempotence_key: str | None = None, payload: dict | None = None) -> dict:
    """Create/replay exactly the persisted request, including offer and return URL."""
    idempotence_key = idempotence_key or str(uuid.uuid4())
    payload = payload if payload is not None else payment_payload(user_id, return_url, idempotence_key)
    if payload.get("amount") != {"value": _rub(PRICE_KOPECKS), "currency": CURRENCY} or payload.get("capture") is not True:
        raise ValueError("Invalid fixed-price checkout request")
    if (payload.get("metadata") or {}).get("user_id") != str(user_id) or (payload.get("metadata") or {}).get("order_key") != idempotence_key:
        raise ValueError("Invalid persisted checkout owner")
    timeout = ClientTimeout(total=20)
    async with ClientSession(timeout=timeout, auth=_auth()) as session:
        async with session.post(
            f"{YOOKASSA_API_URL.rstrip('/')}/payments",
            json=payload,
            headers={"Idempotence-Key": idempotence_key, "Content-Type": "application/json"},
        ) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(f"YooKassa create payment failed: HTTP {response.status}")
            return data


async def get_yookassa_payment(payment_id: str) -> dict:
    """Получает платёж напрямую из ЮKassa для серверной проверки webhook."""
    if not isinstance(payment_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", payment_id):
        raise ValueError("Некорректный payment_id")
    timeout = ClientTimeout(total=20)
    async with ClientSession(timeout=timeout, auth=_auth()) as session:
        async with session.get(
            f"{YOOKASSA_API_URL.rstrip('/')}/payments/{payment_id}"
        ) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(f"YooKassa get payment failed: HTTP {response.status}")
            return data


def validate_payment_identity(payment: dict, user_id: int, payment_id: str | None = None) -> None:
    if not isinstance(payment, dict) or not payment.get("id"):
        raise ValueError("В API отсутствует идентификатор платежа")
    if payment_id is not None and payment.get("id") != payment_id:
        raise ValueError("API вернул другой платёж")
    if payment.get("test") is not TEST_MODE:
        raise ValueError("Режим test/live платежа не соответствует боту")
    if str((payment.get("recipient") or {}).get("account_id")) != str(YOOKASSA_SHOP_ID):
        raise ValueError("Платёж относится к другому магазину")
    amount = payment.get("amount") or {}
    try:
        valid_amount = Decimal(str(amount.get("value"))) == Decimal(_rub(PRICE_KOPECKS))
    except Exception:
        valid_amount = False
    if amount.get("currency") != CURRENCY or not valid_amount:
        raise ValueError("Сумма или валюта платежа не совпадают с заказом")
    if str((payment.get("metadata") or {}).get("user_id")) != str(user_id):
        raise ValueError("Платёж не принадлежит пользователю")


def validate_successful_payment(payment: dict, user_id: int, payment_id: str | None = None) -> None:
    validate_payment_identity(payment, user_id, payment_id)
    if payment.get("status") != "succeeded" or payment.get("paid") is not True:
        raise ValueError("Платёж ещё не имеет статуса succeeded")

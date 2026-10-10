import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "gymochka_admin")

# Production admins: numeric Telegram IDs only; usernames are test-only.
def _parse_csv(value: str) -> set[str]:
    return {item.strip().lstrip("@").lower() for item in value.split(",") if item.strip()}

ADMIN_USERNAMES = _parse_csv(
    os.getenv("ADMIN_USERNAMES", "lysikovvk,gymochka_admin")
)
ADMIN_USER_IDS = {
    int(item) for item in os.getenv("ADMIN_USER_IDS", "").split(",")
    if item.strip().isdigit()
}

SUPPORT_URL = "https://t.me/" + SUPPORT_USERNAME.lstrip("@")

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
PAYMENTS_TOKEN = os.getenv("PAYMENTS_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://yourdomain.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN", "")

TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ("1", "true", "yes")

# Юридические настройки. Перед продакшеном обязательно заполнить реквизитами оператора.
OPERATOR_NAME = os.getenv("OPERATOR_NAME", "Плотников Владимир Константинович")
OPERATOR_ADDRESS = os.getenv("OPERATOR_ADDRESS", "г. Самара, проспект Карла Маркса, 231")
OPERATOR_EMAIL = os.getenv("OPERATOR_EMAIL", "plotnikov.v.k.92@gmail.com")
PRIVACY_POLICY_URL = os.getenv("PRIVACY_POLICY_URL", "")
PUBLIC_OFFER_URL = os.getenv("PUBLIC_OFFER_URL", "")
PERSONAL_DATA_CONSENT_URL = os.getenv("PERSONAL_DATA_CONSENT_URL", "")
MARKETING_CONSENT_URL = os.getenv("MARKETING_CONSENT_URL", "")
PRIVACY_POLICY_VERSION = os.getenv("PRIVACY_POLICY_VERSION", "2026-10-02")
CONSENT_VERSION = os.getenv("CONSENT_VERSION", "2026-10-02")
MARKETING_CONSENT_VERSION = os.getenv("MARKETING_CONSENT_VERSION", "2026-10-02")
PUBLIC_OFFER_VERSION = os.getenv("PUBLIC_OFFER_VERSION", "2026-10-02")
MIN_USER_AGE = int(os.getenv("MIN_USER_AGE", "18"))

# Напоминание о незавершённой покупке: отправляется один раз после 2 часов бездействия
# и только пользователям с отдельным согласием на информационные/рекламные сообщения.
PURCHASE_REMINDER_DELAY_HOURS = float(os.getenv("PURCHASE_REMINDER_DELAY_HOURS", "2"))
PURCHASE_REMINDER_CHECK_SECONDS = int(os.getenv("PURCHASE_REMINDER_CHECK_SECONDS", "60"))

# Позиционирование сервиса: информационно-развлекательный сервис, а не медицинская услуга.
SERVICE_NAME = os.getenv("SERVICE_NAME", "Нямбо")
SERVICE_DISCLAIMER = os.getenv(
    "SERVICE_DISCLAIMER",
    "Информационно-развлекательный сервис для самостоятельного планирования питания. "
    "Расчёты и меню носят справочный характер, не являются медицинской рекомендацией, "
    "диагностикой или назначением лечебной диеты и не заменяют консультацию врача. "
    "Использование сервиса не гарантирует снижение или набор веса.",
)

PRICE_KOPECKS = 8900
CURRENCY = "RUB"
def _project_path(env_name: str, default_relative: str) -> str:
    value = os.getenv(env_name, default_relative)
    return value if os.path.isabs(value) else os.path.join(BASE_DIR, value)


DB_PATH = _project_path("DB_PATH", "bot.db")

# Шрифт для PDF (нужен с кириллицей)
PDF_FONT_PATH = _project_path("PDF_FONT_PATH", os.path.join("fonts", "DejaVuSans.ttf"))
PDF_FONT_BOLD_PATH = _project_path("PDF_FONT_BOLD_PATH", os.path.join("fonts", "DejaVuSans-Bold.ttf"))

ACTIVITY_FACTORS = {
    "sedentary": ("Минимальная (сидячая работа)", 1.2),
    "light": ("Лёгкая (1–3 тренировки/нед)", 1.375),
    "moderate": ("Средняя (3–5 тренировок/нед)", 1.55),
    "high": ("Высокая (6–7 тренировок/нед)", 1.725),
    "extreme": ("Очень высокая (физ. труд)", 1.9),
}

GOALS = {
    "loss": ("Снижение веса", -0.20),
    "maintain": ("Поддержание", 0.00),
    "gain": ("Набор массы", +0.20),
}

# Теги аллергенов/диет
ALLERGEN_TAGS = {
    "gluten": "🌾 Глютен",
    "dairy": "🥛 Молочное",
    "nuts": "🥜 Орехи",
    "seeds": "🌻 Семечки",
    "eggs": "🥚 Яйца",
    "soy": "🫘 Соя",
    "fish": "🐟 Рыба/морепродукты",
    "meat": "🥩 Мясо/птица",
    "honey": "🍯 Мёд",
}
# YooKassa
YOOKASSA_API_URL = os.getenv("YOOKASSA_API_URL", "https://api.yookassa.ru/v3")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "")
YOOKASSA_WEBHOOK_PATH = os.getenv("YOOKASSA_WEBHOOK_PATH", "/yookassa/webhook")

# Preserve calendar-day access, independent of host timezone.
SERVICE_TIMEZONE = os.getenv("SERVICE_TIMEZONE", "Europe/Samara")

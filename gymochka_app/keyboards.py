from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import (
    ACTIVITY_FACTORS, GOALS, ALLERGEN_TAGS, SUPPORT_URL,
    PUBLIC_OFFER_URL, PERSONAL_DATA_CONSENT_URL, MARKETING_CONSENT_URL,
)


def _main_row():
    return [InlineKeyboardButton(text="🏠 Главное меню", callback_data="to_main")]


def _support_row():
    return [InlineKeyboardButton(text="💬 Чат с поддержкой", url=SUPPORT_URL)]


def legal_kb(consent_given: bool = False, marketing_given: bool = False) -> InlineKeyboardMarkup:
    """Юридический экран: документы отдельно, согласия отдельно.

    Согласие на ПДн не объединяется с офертой или маркетинговой рассылкой.
    Маркетинговое согласие добровольное и не требуется для основной услуги.
    """
    rows = []
    if PERSONAL_DATA_CONSENT_URL:
        rows.append([InlineKeyboardButton(text="📄 Согласие на обработку ПДн", url=PERSONAL_DATA_CONSENT_URL)])
    else:
        rows.append([InlineKeyboardButton(text="📄 Согласие на обработку ПДн", callback_data="personal_data_consent_doc")])

    if PUBLIC_OFFER_URL:
        rows.append([InlineKeyboardButton(text="📑 Публичная оферта", url=PUBLIC_OFFER_URL)])
    else:
        rows.append([InlineKeyboardButton(text="📑 Публичная оферта", callback_data="offer_doc")])

    rows.append([InlineKeyboardButton(text="🔐 Политика обработки ПДн", callback_data="privacy")])

    consent_label = "✅ Согласие на обработку ПДн получено" if consent_given else "☐ Я согласен на обработку ПДн"
    rows.append([InlineKeyboardButton(
        text=consent_label,
        callback_data="consent_status" if consent_given else "consent_accept",
    )])

    if marketing_given:
        rows.append([InlineKeyboardButton(
            text="🔕 Отключить рассылку",
            callback_data="marketing_revoke",
        )])
    else:
        rows.append([InlineKeyboardButton(
            text="☐ Получать новости и предложения",
            callback_data="marketing_toggle",
        )])
    rows.append([InlineKeyboardButton(text="📨 Согласие на рассылку", callback_data="marketing_consent_doc")])

    if consent_given:
        rows.append([InlineKeyboardButton(text="▶️ Начать", callback_data="legal_continue")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Совместимость со старым именем функции.
def privacy_kb(policy_url: str = "") -> InlineKeyboardMarkup:
    return legal_kb()


def data_kb(marketing_given: bool = False) -> InlineKeyboardMarkup:
    marketing_text = "🔕 Отказаться от рассылки" if marketing_given else "📨 Разрешить рассылку"
    marketing_cb = "marketing_revoke" if marketing_given else "marketing_toggle"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Политика обработки данных", callback_data="privacy")],
        [InlineKeyboardButton(text=marketing_text, callback_data=marketing_cb)],
        [InlineKeyboardButton(text="🗑 Удалить мои данные", callback_data="delete_data")],
        _main_row(),
    ])


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_support_row(),
        [InlineKeyboardButton(text="🔐 Мои данные", callback_data="my_data")], _main_row()])


def start_kb(price_rub: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Документы и согласия", callback_data="privacy")],
        [InlineKeyboardButton(text="🎁 Бесплатный демо-расчёт на 1 день", callback_data="demo")],
        [InlineKeyboardButton(text=f"💳 Купить план на 30 дней — {price_rub:.0f} ₽", callback_data="pay")],
        _support_row(),
    ])


def after_demo_kb(price_rub: float) -> InlineKeyboardMarkup:
    """Меню после использованного демо: только покупка (демо повторно недоступно)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Купить план на 30 дней — {price_rub:.0f} ₽", callback_data="pay")],
        [InlineKeyboardButton(text="📄 Документы и согласия", callback_data="privacy")],
        _support_row(),
        _main_row(),
    ])


def plan_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Мой план", callback_data="show_plan")],
        [InlineKeyboardButton(text="🛒 Список покупок", callback_data="show_shopping")],
        [InlineKeyboardButton(text="📄 Скачать PDF", callback_data="download_pdf")],
        [InlineKeyboardButton(text="🔄 Пересчитать", callback_data="recalc")],
        _support_row(),
    ])


def plan_overview_kb(days: int = 30) -> InlineKeyboardMarkup:
    """Компактная навигация по длинному плану: сначала недели, затем дни."""
    rows = []
    for start in range(0, days, 7):
        end = min(start + 7, days)
        rows.append([
            InlineKeyboardButton(
                text=f"📅 Дни {start + 1}–{end}",
                callback_data=f"plan_week_{start}",
            )
        ])
    rows.append([
        InlineKeyboardButton(text="📍 Сегодня", callback_data="plan_today"),
        InlineKeyboardButton(text="🛒 Покупки", callback_data="show_shopping"),
    ])
    rows.append([
        InlineKeyboardButton(text="📄 PDF", callback_data="download_pdf"),
        InlineKeyboardButton(text="🔄 Пересчитать", callback_data="recalc"),
    ])
    rows.append(_main_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plan_week_kb(start: int, end: int) -> InlineKeyboardMarkup:
    rows = []
    current = start
    while current < end:
        row = []
        for idx in range(current, min(current + 2, end)):
            row.append(InlineKeyboardButton(
                text=f"День {idx + 1}",
                callback_data=f"plan_day_{idx}",
            ))
        rows.append(row)
        current += 2
    rows.append([InlineKeyboardButton(text="⬅️ К плану", callback_data="show_plan")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plan_day_kb(index: int, total_days: int) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="🍽 Рецепты на день", callback_data=f"day_recipes_{index}"),
        InlineKeyboardButton(text="🛒 Покупки", callback_data="show_shopping"),
    ]]
    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(text="◀️ Предыдущий", callback_data=f"plan_day_{index - 1}"))
    if index + 1 < total_days:
        nav.append(InlineKeyboardButton(text="Следующий ▶️", callback_data=f"plan_day_{index + 1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(text="📅 К плану", callback_data="show_plan"),
        InlineKeyboardButton(text="📄 PDF", callback_data="download_pdf"),
    ])
    rows.append(_main_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pay_step_kb(price_rub: float, test_mode: bool) -> InlineKeyboardMarkup:
    # Free bypass is exposed only in the confirmed test bot.
    rows = [
        [InlineKeyboardButton(text=f"💳 Оплатить {price_rub:.0f} ₽", callback_data="pay_now")],
    ]
    if test_mode:
        rows.append([InlineKeyboardButton(text="🧪 Пропустить оплату (тест)", callback_data="skip_pay")])
    rows.append([InlineKeyboardButton(text="🔄 Проверить оплату", callback_data="check_payment")])
    rows.append([InlineKeyboardButton(text="📑 Публичная оферта", url=PUBLIC_OFFER_URL)] if PUBLIC_OFFER_URL else [InlineKeyboardButton(text="📑 Публичная оферта", callback_data="offer_doc")])
    rows.append(_support_row())
    rows.append(_main_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gender_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨 Мужской", callback_data="gender_m"),
         InlineKeyboardButton(text="👩 Женский", callback_data="gender_f")],
        _main_row(),
    ])


def activity_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"act_{k}")]
            for k, (label, _) in ACTIVITY_FACTORS.items()]
    rows.append(_main_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def goal_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"goal_{k}")]
            for k, (label, _) in GOALS.items()]
    rows.append(_main_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def allergens_kb() -> InlineKeyboardMarkup:
    rows = []
    for tag, label in ALLERGEN_TAGS.items():
        rows.append([InlineKeyboardButton(text=f"❌ {label}", callback_data=f"all_{tag}")])
    rows.append([InlineKeyboardButton(text="✅ Готово (ничего не исключаю)", callback_data="all_done")])
    rows.append(_main_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Выдать доступ на 30 дней", callback_data="admin_grant")],
        [InlineKeyboardButton(text="🔎 Проверить пользователя", callback_data="admin_check")],
        [InlineKeyboardButton(text="❌ Отозвать доступ", callback_data="admin_revoke")],
        _main_row(),
    ])


def admin_user_confirm_kb(action: str, user_id: int) -> InlineKeyboardMarkup:
    if action == "grant":
        label = "🎁 Подтвердить выдачу 30 дней"
        callback = f"admin_grant_confirm:{user_id}"
    else:
        label = "❌ Подтвердить отзыв доступа"
        callback = f"admin_revoke_confirm:{user_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=callback)],
        [InlineKeyboardButton(text="↩️ Отмена", callback_data="admin_panel")],
    ])


def purchase_reminder_kb(price_rub: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Купить план на 30 дней — {price_rub:.0f} ₽", callback_data="pay")],
        [InlineKeyboardButton(text="🔕 Отключить такие сообщения", callback_data="marketing_revoke")],
    ])

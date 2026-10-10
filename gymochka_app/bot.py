import asyncio
import json
import logging
import os
import sys
import tempfile
from urllib.parse import urlsplit
from access import service_today, active_access
from datetime import date, timedelta, datetime, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.filters import CommandStart, Command
from aiogram.types import (Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
                           InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile,
                           BotCommand, BotCommandScopeAllPrivateChats, MenuButtonCommands)
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import DeleteWebhook, SetWebhook

from config import (BOT_TOKEN, PAYMENTS_TOKEN, PRICE_KOPECKS, CURRENCY,
                    WEBHOOK_URL, WEBHOOK_PATH, WEBHOOK_PORT, ALLERGEN_TAGS, TEST_MODE,
                    SUPPORT_URL, ADMIN_USERNAMES, ADMIN_USER_IDS, PRIVACY_POLICY_URL,
                    PUBLIC_OFFER_URL, PERSONAL_DATA_CONSENT_URL, MARKETING_CONSENT_URL,
                    PRIVACY_POLICY_VERSION, CONSENT_VERSION, MARKETING_CONSENT_VERSION, PUBLIC_OFFER_VERSION,
                    MIN_USER_AGE, SERVICE_DISCLAIMER, SERVICE_NAME, WEBHOOK_SECRET_TOKEN, YOOKASSA_RETURN_URL, YOOKASSA_WEBHOOK_PATH, YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY,
                    ACTIVITY_FACTORS, GOALS, OPERATOR_ADDRESS, OPERATOR_EMAIL, BASE_DIR,
                    PURCHASE_REMINDER_DELAY_HOURS, PURCHASE_REMINDER_CHECK_SECONDS)
from states import Onboarding, Admin
from keyboards import (start_kb, gender_kb, activity_kb, goal_kb, allergens_kb,
                       after_demo_kb, plan_menu_kb, plan_overview_kb, plan_week_kb, plan_day_kb, main_menu_kb, pay_step_kb, admin_panel_kb, admin_user_confirm_kb, privacy_kb, legal_kb, data_kb, purchase_reminder_kb)
from database import (
    init_db, upsert_user, get_user, get_user_by_username, delete_user_data, touch_activity,
    get_due_purchase_reminders, get_purchase_reminder_candidate, claim_purchase_reminder, mark_purchase_reminder_sent,
    get_yookassa_intent, get_payment, set_yookassa_intent_status,
    save_plan_if_current, claim_payment_notification, finish_payment_notification, mark_payment_fulfilled, revoke_access, revoke_access_for_grant, get_recoverable_payment_ids, mark_payment_checked,
    claim_payment, get_or_create_yookassa_intent, set_yookassa_intent_payment, complete_yookassa_payment,
)
from nutrition import (
    harris_benedict_bmr, calc_tdee, calc_target, validate_target_kcal, build_plan
)
from pdf_export import generate_pdf
from payment_service import create_yookassa_payment, get_yookassa_payment, validate_successful_payment, validate_payment_identity
from privacy import privacy_policy_text, consent_text, marketing_consent_text, offer_text

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
from sqlite_fsm import SQLiteStorage, SQLiteEventIsolation
dp = Dispatcher(storage=SQLiteStorage(), events_isolation=SQLiteEventIsolation())
reminder_task = None
recovery_task = None

DAY_LABELS = ["🌅 Завтрак", "🍎 Перекус", "☀️ Обед", "🍏 Перекус", "🌙 Ужин"]
PLAN_DAYS = 30
PAYMENT_PAYLOAD = "nutrition_plan_30d"


async def configure_bot_navigation():
    """Регистрирует постоянную навигацию Telegram, независимую от истории чата.

    После очистки истории inline-кнопки старых сообщений исчезают. Команды
    /start и /menu остаются доступными через нативное меню Telegram, а кнопка
    меню чата открывает именно этот список команд.
    """
    commands = [
        BotCommand(command="start", description="Запустить / восстановить бота"),
        BotCommand(command="menu", description="Открыть главное меню"),
        BotCommand(command="privacy", description="Документы и политика данных"),
        BotCommand(command="delete_data", description="Удалить мои данные"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


class ActivityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is not None:
            try:
                await touch_activity(user.id, datetime.now(timezone.utc).isoformat(timespec="seconds"))
                username = (getattr(user, "username", None) or "").strip().lstrip("@").lower()
                if username:
                    await upsert_user(user.id, telegram_username=username)
            except Exception:
                logging.warning("Activity update deferred")
        if user is not None:
            callback = getattr(event, "data", None)
            text = getattr(event, "text", "") or ""
            legal_callbacks = {"privacy", "personal_data_consent_doc", "offer_doc", "marketing_consent_doc", "consent_accept", "consent_status", "legal_continue", "marketing_revoke", "marketing_toggle", "my_data", "delete_data", "to_main"}
            legal_commands = {"/start", "/menu", "/privacy", "/delete_data", "/unsubscribe"}
            exempt = (callback in legal_callbacks or text.split(" ")[0].split("@")[0] in legal_commands or is_admin(user))
            if not exempt and not has_consent(await get_user(user.id)):
                message = getattr(event, "message", None) or event
                await message.answer("Перед продолжением подтвердите согласие на обработку данных.", reply_markup=consent_required_markup(await get_user(user.id)))
                if callback is not None: await safe_answer(event)
                return
        return await handler(event, data)


dp.message.outer_middleware(ActivityMiddleware())
dp.callback_query.outer_middleware(ActivityMiddleware())


async def mark_purchase_reminder_eligible(user_id: int):
    user = await get_user(user_id)
    if not user or user.get("paid") or user.get("purchase_reminder_sent_at"):
        return
    if not user.get("purchase_reminder_eligible_at"):
        await upsert_user(
            user_id,
            purchase_reminder_eligible_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )


def _reminder_cutoff_iso() -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=PURCHASE_REMINDER_DELAY_HOURS)
    return cutoff.isoformat(timespec="seconds")


async def purchase_reminder_loop():
    """Проверяет незавершённые покупки и отправляет максимум одно напоминание на профиль."""
    delay = max(0.1, PURCHASE_REMINDER_DELAY_HOURS)
    logging.info("Purchase reminder loop started: delay=%.2fh", delay)
    while True:
        try:
            cutoff = _reminder_cutoff_iso()
            users = await get_due_purchase_reminders(cutoff)
            for row in users:
                user_id = row["user_id"]
                try:
                    if not await get_purchase_reminder_candidate(user_id, cutoff):
                        continue
                    # Резервируем напоминание ДО отправки: принцип at-most-once.
                    # Если бот будет запущен в двух экземплярах, второй уже не сможет
                    # забрать этого пользователя. Если Telegram временно недоступен,
                    # напоминание сознательно не повторяется — не надоедаем пользователю.
                    claimed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    if not await claim_purchase_reminder(user_id, claimed_at, cutoff):
                        continue
                    current = await get_user(user_id)
                    if not current or current.get("paid") or not current.get("marketing_consent"):
                        continue
                    if (current.get("last_activity_at") or "") > cutoff:
                        continue
                    await bot.send_message(
                        user_id,
                        "⏰ <b>Напоминание от Нямбо</b>\n\n"
                        "Если план питания ещё актуален, его можно оформить на 30 дней за <b>89 ₽</b>.\n\n"
                        "Внутри: меню с рецептами и граммовкой, список покупок и PDF.\n\n"
                        "Это единственное напоминание о покупке. Повторно отправлять его не будем.",
                        parse_mode="HTML",
                        reply_markup=purchase_reminder_kb(PRICE_KOPECKS / 100),
                    )
                    await mark_purchase_reminder_sent(user_id, claimed_at)
                except Exception:
                    logging.warning("Purchase reminder send failed")
        except asyncio.CancelledError:
            logging.info("Purchase reminder loop stopped")
            raise
        except Exception:
            logging.warning("Ошибка purchase reminder loop")
        await asyncio.sleep(max(10, PURCHASE_REMINDER_CHECK_SECONDS))


async def payment_recovery_loop():
    """Recover known checkouts and incomplete fulfillment without webhook delivery."""
    while True:
        try:
            for payment_id in await get_recoverable_payment_ids():
                try:
                    payment = await get_yookassa_payment(payment_id)
                    if payment.get("status") == "succeeded":
                        await process_yookassa_success(payment_id, payment)
                    elif payment.get("status") == "canceled":
                        user_id = int((payment.get("metadata") or {})["user_id"])
                        validate_payment_identity(payment, user_id, payment_id)
                        await set_yookassa_intent_status(user_id, payment_id, "canceled")
                except Exception:
                    logging.warning("Payment recovery deferred")
                finally:
                    await mark_payment_checked(payment_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.warning("Payment recovery loop deferred")
        await asyncio.sleep(60)


async def safe_answer(cb: CallbackQuery, text: str | None = None, show_alert: bool = False):
    """Безопасно закрывает callback-query, даже если Telegram уже протух."""
    try:
        if text is None:
            await cb.answer()
        else:
            await cb.answer(text, show_alert=show_alert)
    except TelegramBadRequest as e:
        msg = str(e).lower()
        if "query is too old" not in msg and "query id is invalid" not in msg:
            logging.warning("Callback answer failed")
    except Exception:
        logging.warning("Unexpected callback answer error")


def has_active_access(user) -> bool:
    return active_access(user)


def can_view_plan(user):
    if has_active_access(user): return True
    if not user or not user.get("demo_used") or user.get("paid"): return False
    try: return len(json.loads(user.get("plan_json") or "[]")) == 1
    except (TypeError, ValueError): return False


def is_demo_only(user) -> bool:
    return bool(user and user.get("demo_used") and not user.get("paid"))


def is_admin(user) -> bool:
    """Production: только закреплённый Telegram ID.
    TEST_MODE: username разрешён как временный способ тестирования.
    """
    user_id = getattr(user, "id", None)
    if user_id is not None and user_id in ADMIN_USER_IDS:
        return True
    if not TEST_MODE:
        return False
    username = (getattr(user, "username", None) or "").lstrip("@").lower()
    return bool(username) and username in ADMIN_USERNAMES_NORMALIZED


ADMIN_USERNAMES_NORMALIZED = {x.lstrip("@").lower() for x in ADMIN_USERNAMES}


def admin_only_message(message: Message) -> bool:
    return is_admin(message.from_user)


def admin_only_callback(cb: CallbackQuery) -> bool:
    return is_admin(cb.from_user)


def _admin_panel_text() -> str:
    return (
        "🛠 <b>Админ-панель Нямбо</b>\n\n"
        "Здесь можно вручную управлять доступом пользователей.\n"
        "Дата регистрации пользователя при этом не меняется.\n\n"
        "👤 Для поиска можно ввести @username или Telegram ID.\n"
        "⚠️ Выдача доступа всегда выполняется только администратором после ручной проверки оплаты."
    )


def has_calculation(user) -> bool:
    return bool(user) and user.get("target_kcal") is not None


def has_consent(user) -> bool:
    return bool(user and user.get("consent_at") and user.get("consent_version") == CONSENT_VERSION)


def consent_required_markup(user=None):
    return legal_kb(
        consent_given=has_consent(user),
        marketing_given=bool(user and user.get("marketing_consent")),
    )


def _plan_period(user):
    """Возвращает (start_date, days) для пересчёта оплаченного плана.
    Сегодняшний день считается днём старого плана, новый начинается завтра."""
    today = service_today()
    raw = user.get("purchase_date")
    if not raw:
        return today, PLAN_DAYS
    purchase = date.fromisoformat(raw)
    elapsed = (today - purchase).days
    if elapsed <= 0:
        return today, PLAN_DAYS
    remaining = PLAN_DAYS - elapsed - 1
    if remaining <= 0:
        return None, 0
    return today + timedelta(days=1), remaining


def _is_new_purchase(user) -> bool:
    """True, если покупки ещё не было или срок 30 дней истёк."""
    raw = user.get("purchase_date") if user else None
    if not raw:
        return True
    return (service_today() - date.fromisoformat(raw)).days >= PLAN_DAYS


async def send_day(chat_message, day, i, total_days=None):
    text = f"📅 <b>День {i}</b> · {day['date']}\n\n"
    for label, meal in zip(DAY_LABELS, day["meals"]):
        prep = f" · ⏱ {meal.get('prep_min')} мин" if meal.get("prep_min") else ""
        text += (f"{label}: <b>{meal['name']}</b>{prep}\n"
                 f"   {meal['kcal']} ккал | Б:{meal['p']} Ж:{meal['f']} У:{meal['c']}\n\n")
    t = day["total"]
    text += f"📊 Итого: {t['kcal']} ккал | Б:{t['p']} Ж:{t['f']} У:{t['c']}"

    if total_days and total_days > 1:
        reply_markup = plan_day_kb(i - 1, total_days)
    else:
        reply_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🍽 Рецепты на день", callback_data=f"day_recipes_{i - 1}")],
            [
                InlineKeyboardButton(text="📄 PDF", callback_data="download_pdf"),
                InlineKeyboardButton(text="🛒 Покупки", callback_data="show_shopping"),
            ],
            [InlineKeyboardButton(text="💬 Поддержка", url=SUPPORT_URL)],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="to_main")],
        ])

    await chat_message.answer(text, parse_mode="HTML", reply_markup=reply_markup)


async def build_plan_and_store(chat_message, user_id: int, target_kcal: float, exclude: set[str], new_purchase: bool = False):
    user = await get_user(user_id)
    today = service_today()
    if not has_active_access(user):
        await chat_message.answer("Доступ к полному плану неактивен.")
        return False
    revision = user.get("plan_revision", 0)
    if new_purchase or not user.get("plan_json"):
        purchase = date.fromisoformat(user["purchase_date"])
        start = max(today, purchase)
        days = PLAN_DAYS - (start - purchase).days
    else:
        start, days = _plan_period(user)
    if days <= 0:
        await chat_message.answer(
            "❌ Твой срок 30 дней закончился.\n"
            "Чтобы получить план снова, оплати план заново — отсчёт начнётся с нуля.",
            reply_markup=pay_step_kb(PRICE_KOPECKS / 100, TEST_MODE))
        return False
    try:
        plan, shopping = await asyncio.to_thread(build_plan, int(target_kcal), days=days, exclude_tags=exclude, start_date=start)
    except ValueError as e:
        await chat_message.answer(
            "⚠️ Не удалось составить план с выбранными исключениями.\n\n"
            f"{e}\n\n"
            "Попробуй изменить исключения и пересчитать план.",
            reply_markup=main_menu_kb(),
        )
        return False
    # Сохраняем только выбранные категории исключений, а не исходные параметры анкеты.
    # Это нужно, чтобы после перезапуска/повторного открытия оплаченного плана
    # бот не построил меню заново с запрещёнными продуктами.
    if not await save_plan_if_current(user_id, revision, json.dumps(plan, ensure_ascii=False), json.dumps(shopping, ensure_ascii=False)):
        return False  # Recalculation, deletion, revocation or another purchase won the race.
    end = start + timedelta(days=days - 1)
    await chat_message.answer(
        f"✅ План готов: {days} дн. ({start.strftime('%d.%m.%Y')} – {end.strftime('%d.%m.%Y')}).\n"
        f"Каждый день = {int(target_kcal)} ккал.\n"
        "Меню, список покупок и PDF — в меню ниже.\n\n"
        f"ℹ️ {SERVICE_DISCLAIMER}",
        reply_markup=plan_menu_kb())
    return True


async def build_and_send_full_plan(chat_message, user_id: int, new_purchase: bool = False):
    user = await get_user(user_id)
    if not user or user.get("target_kcal") is None:
        await chat_message.answer("Сначала нужно выполнить расчёт параметров.")
        return
    # Исключения храним отдельно в минимальном виде (только категории), чтобы
    # повторная генерация после перезапуска не возвращала запрещённые продукты.
    try:
        exclude = set(json.loads(user.get("exclude_tags_json") or "[]"))
    except (TypeError, json.JSONDecodeError):
        exclude = set()
    await build_plan_and_store(
        chat_message, user_id, float(user["target_kcal"]), exclude, new_purchase=new_purchase
    )


def _with_admin_button(markup, is_admin_user):
    if is_admin_user:
        markup.inline_keyboard.insert(0, [InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    return markup


async def send_start_menu(chat_message, user, is_admin_user=False):
    if user and has_active_access(user) and user.get("plan_json"):
        await chat_message.answer("🏠 Главное меню:", reply_markup=_with_admin_button(plan_menu_kb(), is_admin_user))
    elif user and user.get("paid") and not has_active_access(user):
        await mark_purchase_reminder_eligible(user["user_id"])
        await chat_message.answer(
            "⏳ Срок полного доступа закончился.\n\n"
            f"Можно снова оформить план на {PLAN_DAYS} дней.",
            reply_markup=_with_admin_button(after_demo_kb(PRICE_KOPECKS / 100), is_admin_user))
    elif is_demo_only(user):
        await mark_purchase_reminder_eligible(user["user_id"])
        await chat_message.answer(
            "🎁 Бесплатный демо-расчёт уже использован.\n\n"
            "Полный план на 30 дней: меню с рецептами и граммовкой, "
            "список покупок, PDF, бесплатные пересчёты в течение срока.",
            reply_markup=_with_admin_button(after_demo_kb(PRICE_KOPECKS / 100), is_admin_user))
    elif user and has_calculation(user):
        await mark_purchase_reminder_eligible(user["user_id"])
        await chat_message.answer(
            "📊 Расчёт уже выполнен. Можно продолжить и оформить полный план на 30 дней.",
            reply_markup=_with_admin_button(after_demo_kb(PRICE_KOPECKS / 100), is_admin_user))
    else:
        await chat_message.answer(
            f"👋 Привет! Я {SERVICE_NAME} — информационно-развлекательный сервис для самостоятельного планирования питания.\n\n"
            f"ℹ️ {SERVICE_DISCLAIMER}\n\n"
            "🎁 Попробуй БЕСПЛАТНО: демо-расчёт и меню на 1 день (1 раз)\n"
            "💳 Полный план на 30 дней: меню с рецептами, список покупок, PDF",
            reply_markup=_with_admin_button(start_kb(PRICE_KOPECKS / 100), is_admin_user))


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer(
            f"👋 <b>Это официальный бот {SERVICE_NAME}.</b>\n\n"
            "Внутри — персональный информационный план питания, рецепты, список покупок и PDF.\n\n"
            "Перед началом ознакомься с документами. Согласие на обработку персональных данных подтверждается отдельным действием. "
            "Согласие на информационные и рекламные сообщения добровольное и не требуется для основной услуги.",
            parse_mode="HTML", reply_markup=consent_required_markup(None))
        return
    if not has_consent(user):
        await message.answer(
            "🔐 Для продолжения нужно подтвердить актуальную версию отдельного согласия на обработку персональных данных.",
            reply_markup=consent_required_markup(user))
        return

    try:
        intent = await get_yookassa_intent(message.from_user.id)
        if intent and intent.get("payment_id") and intent["status"] == "pending":
            payment = await get_yookassa_payment(intent["payment_id"])
            validate_payment_identity(payment, message.from_user.id, intent["payment_id"])
            if payment.get("status") == "succeeded":
                await process_yookassa_success(intent["payment_id"], payment)
            elif payment.get("status") == "canceled":
                await set_yookassa_intent_status(message.from_user.id, intent["payment_id"], "canceled")
        user = await get_user(message.from_user.id)
    except Exception:
        logging.warning("Payment recovery deferred")
    if user and has_active_access(user) and not user.get("plan_json"):
        if has_calculation(user):
            await message.answer("🎁 Доступ активен. Строю твой 30-дневный план...")
            await build_and_send_full_plan(message, message.from_user.id, new_purchase=False)
            return
        await state.update_data(wants_full=True)
        await message.answer(
            "🎁 Доступ на 30 дней уже активен.\n\n"
            "Заполним параметры и построим твой персональный план.\n\n"
            "Введи свой возраст (полных лет):",
            reply_markup=main_menu_kb())
        await state.set_state(Onboarding.waiting_age)
        return
    await send_start_menu(message, user, is_admin(message.from_user))


@dp.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    """Recovery-навигация после очистки истории Telegram.

    /menu намеренно использует тот же безопасный путь, что и /start:
    профиль берётся из БД, временное FSM-состояние очищается, а для нового
    пользователя сохраняется обычный стартовый onboarding.
    """
    await cmd_start(message, state)


@dp.callback_query(F.data == "privacy")
async def show_privacy(cb: CallbackQuery):
    await safe_answer(cb)
    user = await get_user(cb.from_user.id)
    await cb.message.answer(
        privacy_policy_text(), parse_mode="HTML",
        reply_markup=consent_required_markup(user),
    )


@dp.callback_query(F.data == "personal_data_consent_doc")
async def show_personal_data_consent(cb: CallbackQuery):
    await safe_answer(cb)
    user = await get_user(cb.from_user.id)
    await cb.message.answer(
        consent_text(), parse_mode="HTML",
        reply_markup=consent_required_markup(user),
    )


@dp.callback_query(F.data == "offer_doc")
async def show_offer_doc(cb: CallbackQuery):
    await safe_answer(cb)
    user = await get_user(cb.from_user.id)
    await cb.message.answer(offer_text(), parse_mode="HTML", reply_markup=consent_required_markup(user))


@dp.callback_query(F.data == "marketing_consent_doc")
async def show_marketing_consent_doc(cb: CallbackQuery):
    await safe_answer(cb)
    user = await get_user(cb.from_user.id)
    await cb.message.answer(
        marketing_consent_text(), parse_mode="HTML",
        reply_markup=consent_required_markup(user),
    )


@dp.callback_query(F.data == "consent_accept")
async def accept_consent(cb: CallbackQuery, state: FSMContext):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await safe_answer(cb, "Согласие сохранено")
    await state.clear()
    await upsert_user(
        cb.from_user.id,
        consent_at=now,
        consent_version=CONSENT_VERSION,
        policy_version=PRIVACY_POLICY_VERSION,
    )
    user = await get_user(cb.from_user.id)
    await cb.message.answer(
        "✅ <b>Отдельное согласие на обработку персональных данных сохранено.</b>\n\n"
        "Рассылка остаётся необязательной — её можно разрешить или не разрешать отдельно.",
        parse_mode="HTML", reply_markup=consent_required_markup(user),
    )


@dp.callback_query(F.data == "consent_status")
async def consent_status(cb: CallbackQuery):
    await safe_answer(cb, "Согласие уже подтверждено")


@dp.callback_query(F.data == "legal_continue")
async def legal_continue(cb: CallbackQuery, state: FSMContext):
    user = await get_user(cb.from_user.id)
    if not has_consent(user):
        await safe_answer(cb, "Сначала дайте отдельное согласие", show_alert=True)
        return
    await safe_answer(cb)
    await state.clear()
    await send_start_menu(cb.message, user, is_admin(cb.from_user))


@dp.callback_query(F.data == "marketing_toggle")
async def marketing_toggle(cb: CallbackQuery):
    """Включает добровольную рассылку и сразу обновляет текущую клавиатуру.

    Важно: кнопка должна давать видимый результат даже на старом сообщении,
    которое пользователь открыл после очистки/восстановления истории. Поэтому
    после записи в БД сначала обновляем клавиатуру самого сообщения, а если
    Telegram больше не позволяет редактировать это старое сообщение —
    отправляем новый экран согласий как запасной вариант.
    """
    user = await get_user(cb.from_user.id)
    if not has_consent(user):
        await safe_answer(cb, "Сначала подтвердите согласие на обработку ПДн", show_alert=True)
        return

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await upsert_user(
        cb.from_user.id,
        marketing_consent=1,
        marketing_consent_at=now,
        marketing_consent_version=MARKETING_CONSENT_VERSION,
        marketing_opt_out_at=None,
    )
    user = await get_user(cb.from_user.id)
    markup = consent_required_markup(user)

    # Сначала закрываем callback: даже если Telegram не позволит изменить
    # старое сообщение, пользователь сразу получает визуальный отклик.
    await safe_answer(cb, "Рассылка разрешена")
    try:
        await cb.message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest as e:
        # Это безопасный idempotent-сценарий: старое сообщение уже содержит
        # актуальную клавиатуру. Если Telegram считает сообщение неизменённым,
        # показываем отдельный актуальный экран, чтобы пользователь не сомневался
        # в результате первого нажатия.
        if "message is not modified" in str(e).lower():
            await cb.message.answer(
                "📨 <b>Рассылка разрешена.</b>\n\n"
                "Согласие сохранено. Его можно отменить в разделе «Мои данные».",
                parse_mode="HTML", reply_markup=markup,
            )
        else:
            logging.warning("Не удалось обновить клавиатуру согласия на рассылку")
            await cb.message.answer(
                "📨 <b>Согласие сохранено.</b>\n\n"
                "Не удалось обновить старое сообщение, поэтому показываю актуальный экран.",
                parse_mode="HTML", reply_markup=markup,
            )
    except Exception:
        logging.warning("Не удалось обновить клавиатуру согласия на рассылку")
        await cb.message.answer(
            "📨 <b>Согласие сохранено.</b>\n\n"
            "Показываю актуальный экран согласия.",
            parse_mode="HTML", reply_markup=markup,
        )


@dp.callback_query(F.data == "marketing_revoke")
async def marketing_revoke(cb: CallbackQuery):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await upsert_user(cb.from_user.id, marketing_consent=0, marketing_opt_out_at=now)
    user = await get_user(cb.from_user.id)
    await safe_answer(cb, "Рассылка отключена")
    await cb.message.answer(
        "🔕 <b>Рассылка отключена.</b>\n\nОсновной доступ к Нямбо от этого не изменяется.",
        parse_mode="HTML", reply_markup=data_kb(False),
    )


@dp.message(Command("unsubscribe"))
async def unsubscribe_command(message: Message):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Профиль ещё не создан.")
        return
    await upsert_user(message.from_user.id, marketing_consent=0, marketing_opt_out_at=now)
    await message.answer("🔕 Рекламные и информационные рассылки отключены. Основной доступ к Нямбо не изменён.")


@dp.message(Command("privacy"))
async def privacy_command(message: Message):
    user = await get_user(message.from_user.id)
    await message.answer(privacy_policy_text(), parse_mode="HTML", reply_markup=consent_required_markup(user))


@dp.callback_query(F.data == "my_data")
async def my_data(cb: CallbackQuery):
    user = await get_user(cb.from_user.id)
    if not user:
        await cb.answer("Профиль ещё не создан", show_alert=True)
        return
    await safe_answer(cb)
    await cb.message.answer(
        "🔐 <b>Мои данные</b>\n\n"
        f"Telegram ID: <code>{cb.from_user.id}</code>\n"
        f"Username: @{user.get('telegram_username') or 'нет'}\n\n"
        f"Согласие: {'актуально' if has_consent(user) else 'не подтверждено'}\n"
        f"Версия согласия: {user.get('consent_version') or '—'}\n"
        f"Дата согласия: {user.get('consent_at') or '—'}\n\n"
        "Если хочешь удалить профиль и сохранённые данные из базы Нямбо, используй кнопку ниже.",
        parse_mode="HTML", reply_markup=data_kb(bool(user.get("marketing_consent")))
    )


@dp.callback_query(F.data == "delete_data")
async def delete_data_callback(cb: CallbackQuery, state: FSMContext):
    await safe_answer(cb)
    await state.clear()
    deleted = await delete_user_data(cb.from_user.id)
    pdf_paths = (
        os.path.join(BASE_DIR, "plans", f"{cb.from_user.id}.pdf"),
        os.path.join(BASE_DIR, "plans", f".tmp_{cb.from_user.id}.pdf"),
    )
    try:
        for pdf_path in pdf_paths:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)
    except OSError:
        logging.warning("PDF cleanup deferred")
    if deleted:
        await cb.message.answer("🗑 Твои данные из локальной базы Нямбо удалены. Чтобы пользоваться ботом снова, запусти /start.")
    else:
        await cb.message.answer("В локальной базе Нямбо данных профиля не найдено.")


@dp.message(Command("delete_data"))
async def delete_data_command(message: Message, state: FSMContext):
    await state.clear()
    deleted = await delete_user_data(message.from_user.id)
    pdf_paths = (
        os.path.join(BASE_DIR, "plans", f"{message.from_user.id}.pdf"),
        os.path.join(BASE_DIR, "plans", f".tmp_{message.from_user.id}.pdf"),
    )
    try:
        for pdf_path in pdf_paths:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)
    except OSError:
        logging.warning("PDF cleanup deferred")
    if deleted:
        await message.answer("🗑 Твои данные из локальной базы Нямбо удалены. Чтобы пользоваться ботом снова, запусти /start.")
    else:
        await message.answer("В локальной базе Нямбо данных профиля не найдено.")


@dp.callback_query(F.data == "to_main")
async def go_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    user = await get_user(cb.from_user.id)
    if not has_consent(user):
        await cb.answer("Сначала подтвердите согласие", show_alert=True)
        await cb.message.answer("🔐 Нужно подтвердить актуальное согласие на обработку данных.", reply_markup=consent_required_markup(user))
        return
    await send_start_menu(cb.message, user, is_admin(cb.from_user))
    await safe_answer(cb)


@dp.message(Command("admin"))
async def admin_command(message: Message, state: FSMContext):
    if not admin_only_message(message):
        await message.answer("⛔ Доступ запрещён.")
        return
    await state.clear()
    await message.answer(_admin_panel_text(), parse_mode="HTML", reply_markup=admin_panel_kb())


@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: CallbackQuery, state: FSMContext):
    if not admin_only_callback(cb):
        await cb.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await safe_answer(cb)
    await state.clear()
    await cb.message.answer(_admin_panel_text(), parse_mode="HTML", reply_markup=admin_panel_kb())


@dp.callback_query(F.data.in_({"admin_grant", "admin_check", "admin_revoke"}))
async def admin_action_start(cb: CallbackQuery, state: FSMContext):
    if not admin_only_callback(cb):
        await cb.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await safe_answer(cb)
    action = cb.data.replace("admin_", "")
    await state.clear()
    await state.update_data(admin_action=action)
    prompts = {
        "grant": "🎁 Введи @username или Telegram ID пользователя, которому выдать доступ на 30 дней.",
        "check": "🔎 Введи @username или Telegram ID пользователя для проверки.",
        "revoke": "❌ Введи @username или Telegram ID пользователя, у которого нужно отозвать доступ.",
    }
    await cb.message.answer(prompts[action])
    await state.set_state(Admin.waiting_user_id)


@dp.message(Admin.waiting_user_id)
async def admin_process_user_id(message: Message, state: FSMContext):
    if not admin_only_message(message):
        await state.clear()
        await message.answer("⛔ Доступ запрещён.")
        return

    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Введи @username или Telegram ID, например @ivan или 123456789.")
        return

    data = await state.get_data()
    action = data.get("admin_action")
    target_id = None
    user = None
    if raw.lstrip("@").isdigit():
        target_id = int(raw.lstrip("@"))
        user = await get_user(target_id)
    else:
        username = raw.lstrip("@").lower()
        if not (5 <= len(username) <= 32) or not all(ch.isalnum() or ch == "_" for ch in username):
            await message.answer("Введи корректный @username или числовой Telegram ID.")
            return
        user = await get_user_by_username(username)
        if user:
            target_id = int(user["user_id"])

    if not user:
        if raw.startswith("@"):
            await message.answer(
                "🔎 Пользователь с таким @username не найден среди зарегистрированных в Нямбо.\n\n"
                "Попроси клиента открыть бота и нажать /start. Если username отсутствует, клиент может открыть «🔐 Мои данные» и прислать тебе свой Telegram ID.")
        else:
            await message.answer(f"🔎 Пользователь <code>{raw}</code> не найден среди зарегистрированных в Нямбо.", parse_mode="HTML")
        return

    if action == "check":
        await message.answer(
            "🔎 <b>Пользователь</b>\n\n"
            f"Username: @{user.get('telegram_username') or '—'}\n"
            f"ID: <code>{target_id}</code>\n"
            f"Регистрация: {user.get('created_at') or '—'}\n"
            f"Доступ: {'✅ активен' if user.get('paid') else '❌ нет'}\n"
            f"Начало периода: {user.get('purchase_date') or '—'}\n"
            f"Согласие: {'да' if has_consent(user) else 'нет'}",
            parse_mode="HTML")
        await state.clear()
        return

    if action in {"grant", "revoke"}:
        status = "✅ активен" if user.get("paid") else "❌ нет"
        action_text = (
            "🎁 <b>Проверка перед выдачей доступа</b>" if action == "grant"
            else "❌ <b>Проверка перед отзывом доступа</b>"
        )
        await message.answer(
            f"{action_text}\n\n"
            f"Username: @{user.get('telegram_username') or '—'}\n"
            f"Telegram ID: <code>{target_id}</code>\n"
            f"Регистрация: {user.get('created_at') or '—'}\n"
            f"Текущий доступ: {status}\n"
            f"Начало текущего периода: {user.get('purchase_date') or '—'}\n\n"
            "⚠️ Убедись, что оплата проверена вручную. Только после этого подтверждай действие.",
            parse_mode="HTML", reply_markup=admin_user_confirm_kb(action, target_id))
        await state.clear()
        return

    await state.clear()


@dp.callback_query(F.data.startswith("admin_grant_confirm:"))
async def admin_grant_confirm(cb: CallbackQuery):
    if not admin_only_callback(cb):
        await cb.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        target_id = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer("Некорректный пользователь", show_alert=True)
        return
    user = await get_user(target_id)
    if not user:
        await cb.answer("Пользователь больше не найден", show_alert=True)
        return

    await safe_answer(cb)
    today = service_today().isoformat()
    if has_calculation(user):
        try:
            exclude = set(json.loads(user.get("exclude_tags_json") or "[]"))
        except (TypeError, json.JSONDecodeError):
            exclude = set()
        try:
            plan, shopping = build_plan(
                int(user["target_kcal"]), days=PLAN_DAYS,
                exclude_tags=exclude, start_date=service_today()
            )
        except ValueError as e:
            await cb.message.answer(
                "⚠️ Доступ не выдан: готовый план создать не удалось.\n\n"
                f"{e}\n"
                "Пользователю нужно изменить исключения и выполнить пересчёт.")
            return
        await revoke_access_for_grant(target_id, today)
        user = await get_user(target_id)
        if not await save_plan_if_current(target_id, user.get("plan_revision", 0), json.dumps(plan, ensure_ascii=False), json.dumps(shopping, ensure_ascii=False)):
            await cb.message.answer("Доступ изменился параллельно. Проверьте профиль повторно.")
            return
        target_text = (
            "🎁 Доступ выдан вручную администратором.\n"
            "Твой персональный план на 30 дней уже готов. Нажми /start, чтобы открыть его."
        )
    else:
        await revoke_access_for_grant(target_id, today)
        target_text = (
            "🎁 Доступ к полному плану на 30 дней выдан.\n"
            "Нажми /start и заполни параметры, чтобы бот построил персональный план."
        )

    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(
        f"✅ Доступ выдан пользователю <code>{target_id}</code> на 30 дней.\n"
        f"Начало периода: {service_today().strftime('%d.%m.%Y')}\n"
        "Дата регистрации пользователя не изменена.",
        parse_mode="HTML",
        reply_markup=admin_panel_kb(),
    )
    try:
        await bot.send_message(target_id, target_text)
    except Exception:
        await cb.message.answer(
            "⚠️ Не смог отправить уведомление пользователю. Возможно, он ещё не запускал бота. "
            "Доступ в базе при этом выдан.")


@dp.callback_query(F.data.startswith("admin_revoke_confirm:"))
async def admin_revoke_confirm(cb: CallbackQuery):
    if not admin_only_callback(cb):
        await cb.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        target_id = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer("Некорректный пользователь", show_alert=True)
        return
    user = await get_user(target_id)
    if not user:
        await cb.answer("Пользователь больше не найден", show_alert=True)
        return
    await safe_answer(cb)
    await revoke_access(target_id)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(
        f"❌ Доступ для <code>{target_id}</code> отозван.",
        parse_mode="HTML",
        reply_markup=admin_panel_kb(),
    )
    try:
        await bot.send_message(target_id, "❌ Доступ к полному плану был отозван. Если это ошибка — напиши в поддержку.")
    except Exception:
        pass


@dp.callback_query(F.data == "demo")
async def start_demo(cb: CallbackQuery, state: FSMContext):
    user = await get_user(cb.from_user.id)
    if user and (user.get("demo_used") or user.get("paid")):
        await cb.answer("🎁 Бесплатный демо-расчёт для этого профиля уже недоступен.", show_alert=True)
        return
    await state.clear()
    await state.update_data(wants_full=False)
    await cb.message.edit_text(
        "🎁 Начнём с расчёта. Это займёт несколько шагов.\n\n"
        "Сначала введи параметры: возраст → рост → вес → пол → активность → цель → ограничения.\n"
        "После расчёта покажу, что входит в демо или полный план.\n\n"
        "Введи возраст (полных лет):",
        reply_markup=main_menu_kb())
    await state.set_state(Onboarding.waiting_age)
    await safe_answer(cb)


@dp.callback_query(F.data == "pay")
async def process_pay(cb: CallbackQuery, state: FSMContext):
    # Закрываем callback ДО DB/Telegram операций, иначе медленный запрос или
    # незначительное редактирование сообщения может сделать callback протухшим.
    await safe_answer(cb)
    user = await get_user(cb.from_user.id)
    if user and has_active_access(user):
        await safe_answer(cb, "У тебя уже есть полный план!", show_alert=True)
        return

    if has_calculation(user):
        await mark_purchase_reminder_eligible(cb.from_user.id)
        await state.clear()
        await cb.message.answer(
            "💳 Финальный шаг — оплата плана на 30 дней.\n"
            "Нажми «Оплатить» ниже. Если возникнет вопрос по оплате — напиши в поддержку:",
            reply_markup=pay_step_kb(PRICE_KOPECKS / 100, TEST_MODE))
        return

    await state.clear()
    await state.update_data(wants_full=True)
    onboarding_text = (
        "💳 Начнём с расчёта. Сначала введём параметры, а уже после расчёта покажу содержимое плана и условия оплаты.\n\n"
        "Введи возраст (полных лет):"
    )
    onboarding_markup = main_menu_kb()
    try:
        await cb.message.edit_text(onboarding_text, reply_markup=onboarding_markup)
    except TelegramBadRequest as e:
        # Повторное/старое нажатие на кнопку может привести к тому, что
        # сообщение уже содержит ровно этот экран. Это НЕ ошибка сценария.
        if "message is not modified" in str(e).lower():
            await cb.message.answer(onboarding_text, reply_markup=onboarding_markup)
        else:
            raise
    await state.set_state(Onboarding.waiting_age)


@dp.callback_query(F.data == "pay_now")
async def pay_now(cb: CallbackQuery):
    user_id = cb.from_user.id
    await safe_answer(cb)
    user = await get_user(user_id)
    if not has_consent(user) or not has_calculation(user):
        await cb.message.answer("Сначала подтвердите согласие и завершите расчёт через /start.")
        return
    if has_active_access(user):
        await cb.message.answer("Доступ уже активен. Повторная оплата не нужна.")
        return
    try:
        intent = await get_or_create_yookassa_intent(user_id)
        if intent["payment_id"]:
            payment = await get_yookassa_payment(intent["payment_id"])
        else:
            # YooKassa stores an idempotence key for 24h. Never reuse a lost
            # response key after that window: an old payment might still exist.
            created = datetime.fromisoformat(intent["created_at"]).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - created >= timedelta(hours=23):
                await cb.message.answer("Статус старого счёта требует проверки поддержки: @gymochka_admin. Новый счёт пока не создаю.")
                return
            if not intent.get("request"):
                await cb.message.answer("Старый запрос без сохранённых параметров требует проверки поддержки. Новый счёт не создаю.")
                return
            payment = await create_yookassa_payment(user_id, intent["request"]["confirmation"]["return_url"], intent["idempotence_key"], payload=intent["request"])
        validate_payment_identity(payment, user_id, intent.get("payment_id"))
        payment_id = payment["id"]
        if not await set_yookassa_intent_payment(user_id, payment_id, intent["idempotence_key"]):
            latest = await get_yookassa_intent(user_id)
            if not latest or latest.get("payment_id") != payment_id:
                raise ValueError("Заказ изменился параллельно")
        status = payment.get("status")
        if status == "succeeded":
            await process_yookassa_success(payment_id, payment)
            await cb.message.answer("Платёж подтверждён. Откройте /start.")
            return
        if status == "canceled":
            await set_yookassa_intent_status(user_id, payment_id, "canceled")
            await cb.message.answer("Счёт отменён. Нажмите «Оплатить» ещё раз для нового счёта.", reply_markup=pay_step_kb(89, TEST_MODE))
            return
        if status == "waiting_for_capture":
            await cb.message.answer("Платёж ожидает завершения. Доступ будет выдан после succeeded; обратитесь в поддержку.")
            return
        if status != "pending": raise ValueError("Неизвестный статус платежа")
        url = (payment.get("confirmation") or {}).get("confirmation_url", "")
        if not url.startswith("https://"): raise ValueError("Нет безопасной ссылки оплаты")
        await cb.message.answer("💳 План на 30 дней — 89 ₽. Оплата на странице ЮKassa.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить 89 ₽", url=url)],
            [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data="check_payment")],
        ]))
    except Exception:
        logging.warning("Payment checkout deferred")
        await cb.message.answer("Не удалось проверить или создать счёт. Повторите нажатие: незавершённый запрос использует тот же заказ.")


@dp.callback_query(F.data == "check_payment")
async def check_payment(cb: CallbackQuery):
    await safe_answer(cb)
    intent = await get_yookassa_intent(cb.from_user.id)
    if not intent or not intent.get("payment_id"):
        await cb.message.answer("Счёт не найден. Откройте /start.")
        return
    try:
        payment = await get_yookassa_payment(intent["payment_id"])
        validate_payment_identity(payment, cb.from_user.id, intent["payment_id"])
        if payment.get("status") == "succeeded":
            await process_yookassa_success(intent["payment_id"], payment)
            await cb.message.answer("Оплата подтверждена. Откройте /start.")
        elif payment.get("status") == "canceled":
            await set_yookassa_intent_status(cb.from_user.id, intent["payment_id"], "canceled")
            await cb.message.answer("Платёж отменён. Можно создать новый счёт.")
        else:
            await cb.message.answer("Платёж пока не завершён. Доступ не изменён.")
    except Exception:
        await cb.message.answer("Проверка временно недоступна. Повторите позже.")


class _BotMessageProxy:
    def __init__(self, user_id: int, silent=False):
        self.user_id, self.silent = user_id, silent

    async def answer(self, text, *args, **kwargs):
        if not self.silent:
            return await bot.send_message(self.user_id, text, *args, **kwargs)


async def process_yookassa_success(payment_id: str, verified_payment=None) -> bool:
    payment = verified_payment if verified_payment is not None else await get_yookassa_payment(payment_id)
    try: user_id = int((payment.get("metadata") or {})["user_id"])
    except (KeyError, TypeError, ValueError): raise ValueError("Некорректный владелец платежа")
    validate_successful_payment(payment, user_id, payment_id)
    existing = await get_payment(payment_id)
    if not existing:
        intent = await get_yookassa_intent(user_id)
        if not intent or intent["status"] != "pending": raise ValueError("Заказ не найден")
        if intent.get("payment_id"):
            if intent["payment_id"] != payment_id: raise ValueError("Платёж не относится к текущему заказу")
        elif (payment.get("metadata") or {}).get("order_key") != intent["idempotence_key"]:
            raise ValueError("Не подтверждена связь платежа и заказа")
    await complete_yookassa_payment(user_id, payment_id, service_today().isoformat(),
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        str((payment.get("metadata") or {}).get("offer_version") or PUBLIC_OFFER_VERSION))
    user = await get_user(user_id)
    # Immutable replay must never resurrect a deleted/revoked/expired period,
    # or send success for a different purchase. Old migrated payments also stop here.
    record = await get_payment(payment_id)
    if not has_active_access(user) or user.get("last_payment_id") != payment_id or record.get("access_revision") is None:
        return False
    if has_calculation(user) and not user.get("plan_json"):
        exclude = set(json.loads(user.get("exclude_tags_json") or "[]"))
        if not await build_plan_and_store(_BotMessageProxy(user_id, silent=True), user_id, float(user["target_kcal"]), exclude):
            raise RuntimeError("Доступ сохранён; построение плана требует повторения")
    await mark_payment_fulfilled(payment_id)
    user = await get_user(user_id)
    if not has_active_access(user) or user.get("last_payment_id") != payment_id:
        return False
    if await claim_payment_notification(payment_id):
        try:
            await bot.send_message(user_id, "✅ Оплата 89 ₽ подтверждена!\n" +
                ("Меню, рецепты, покупки и PDF доступны через /start." if user.get("plan_json") else "Завершите анкету через /start."))
        except TelegramNetworkError:
            # Ambiguous delivery: do not retry automatically and duplicate a message.
            # Access/plan are durable and recoverable through /start.
            logging.warning("Payment notification delivery unknown; access retained")
        except Exception:
            await finish_payment_notification(payment_id, False)
            raise
        else:
            await finish_payment_notification(payment_id, True)
    return True


@dp.callback_query(F.data == "skip_pay")
async def skip_pay(cb: CallbackQuery, state: FSMContext):
    await safe_answer(cb)
    if not TEST_MODE:
        await cb.message.answer("Пропуск оплаты недоступен.")
        return

    user = await get_user(cb.from_user.id)
    try:
        exclude = set(json.loads(user.get("exclude_tags_json") or "[]")) if user else set()
    except (TypeError, json.JSONDecodeError):
        exclude = set()
    if not user or user.get("target_kcal") is None:
        await cb.message.answer(
            "Сначала нужно завершить расчёт параметров, затем можно продолжить к оплате."
        )
        return

    new_purchase = _is_new_purchase(user)
    await revoke_access_for_grant(cb.from_user.id, service_today().isoformat())
    new_purchase = True
    await cb.message.answer("🧪 Тестовый режим: оплата пропущена. Строю план...")

    user = await get_user(cb.from_user.id)
    if user and user.get("target_kcal") is not None:
        await build_plan_and_store(
            cb.message, cb.from_user.id, float(user["target_kcal"]),
            exclude, new_purchase=new_purchase
        )
    else:
        await cb.message.answer("Сначала нужно завершить расчёт параметров.")
    await state.clear()


@dp.pre_checkout_query()
async def pre_checkout(pre: PreCheckoutQuery):
    await pre.answer(ok=False, error_message="Используйте оплату на странице ЮKassa через меню бота.")


@dp.message(F.successful_payment)
async def success_payment(message: Message, state: FSMContext):
    await message.answer("Получен платёж по старому способу оплаты. Обратитесь в @gymochka_admin для проверки; автоматический доступ выдаётся через ЮKassa.")


@dp.message(Onboarding.waiting_age, F.text.regexp(r"^\d{2,3}$"))
async def get_age(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not has_consent(user):
        await message.answer("🔐 Сначала подтверди согласие на обработку данных.", reply_markup=consent_required_markup(user))
        return
    age = int(message.text)
    if not (MIN_USER_AGE <= age <= 100):
        return await message.answer(f"Сервис доступен пользователям от {MIN_USER_AGE} лет.", reply_markup=main_menu_kb())
    await state.update_data(age=age)
    await message.answer("📏 Введи рост в см:", reply_markup=main_menu_kb())
    await state.set_state(Onboarding.waiting_height)


@dp.message(Onboarding.waiting_age)
async def bad_age(message: Message):
    await message.answer(f"Укажи возраст числом, например: 28. Сервис доступен пользователям от {MIN_USER_AGE} лет.", reply_markup=main_menu_kb())


@dp.message(Onboarding.waiting_height, F.text.regexp(r"^\d{3}$"))
async def get_height(message: Message, state: FSMContext):
    h = int(message.text)
    if not (120 <= h <= 230):
        return await message.answer("Рост: 120–230 см.", reply_markup=main_menu_kb())
    await state.update_data(height=h)
    await message.answer("⚖️ Введи вес в кг (можно 72.5):", reply_markup=main_menu_kb())
    await state.set_state(Onboarding.waiting_weight)


@dp.message(Onboarding.waiting_height)
async def bad_height(message: Message):
    await message.answer("Нужно трёхзначное число, например: 175", reply_markup=main_menu_kb())


@dp.message(Onboarding.waiting_weight, F.text.regexp(r"^\d{2,3}(\.\d)?$"))
async def get_weight(message: Message, state: FSMContext):
    w = float(message.text)
    if not (35 <= w <= 250):
        return await message.answer("Вес: 35–250 кг.", reply_markup=main_menu_kb())
    await state.update_data(weight=w)
    await message.answer("Укажи пол:", reply_markup=gender_kb())
    await state.set_state(Onboarding.waiting_gender)


@dp.message(Onboarding.waiting_weight)
async def bad_weight(message: Message):
    await message.answer("Пример: 72 или 72.5", reply_markup=main_menu_kb())


@dp.callback_query(F.data.startswith("gender_"), Onboarding.waiting_gender)
async def get_gender(cb: CallbackQuery, state: FSMContext):
    if cb.data not in {"gender_m", "gender_f"}:
        await safe_answer(cb, "Некорректный вариант.", show_alert=True)
        return
    gender = "m" if cb.data == "gender_m" else "f"
    await state.update_data(gender=gender)
    await cb.message.edit_text("Выбери уровень активности:", reply_markup=activity_kb())
    await state.set_state(Onboarding.waiting_activity)
    await safe_answer(cb)


@dp.callback_query(F.data.startswith("act_"), Onboarding.waiting_activity)
async def get_activity(cb: CallbackQuery, state: FSMContext):
    key = cb.data.replace("act_", "")
    if key not in ACTIVITY_FACTORS:
        await safe_answer(cb, "Некорректный вариант.", show_alert=True)
        return
    await state.update_data(activity=key)
    await cb.message.edit_text("Выбери цель:", reply_markup=goal_kb())
    await state.set_state(Onboarding.waiting_goal)
    await safe_answer(cb)


@dp.callback_query(F.data.startswith("goal_"), Onboarding.waiting_goal)
async def get_goal(cb: CallbackQuery, state: FSMContext):
    key = cb.data.replace("goal_", "")
    if key not in GOALS:
        await safe_answer(cb, "Некорректный вариант.", show_alert=True)
        return
    await state.update_data(goal=key, allergens=[])
    await cb.message.edit_text(
        f"🍽 Есть ли продукты, которые нужно исключить из меню?\n\nℹ️ {SERVICE_DISCLAIMER}\n\n"
        "Не указывай здесь диагнозы или медицинские сведения. Нажми на продукты, которые нужно исключить, или «Готово»:",
        reply_markup=allergens_kb())
    await state.set_state(Onboarding.waiting_allergens)
    await safe_answer(cb)


@dp.callback_query(F.data.startswith("all_"), Onboarding.waiting_allergens)
async def toggle_allergen(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    current = set(data.get("allergens") or [])
    tag = cb.data.replace("all_", "")

    if tag == "done":
        return await finalize(cb, state)
    if tag not in ALLERGEN_TAGS:
        await safe_answer(cb, "Некорректный вариант.", show_alert=True)
        return

    if tag in current:
        current.remove(tag)
    else:
        current.add(tag)

    await state.update_data(allergens=list(current))
    excluded = ", ".join(ALLERGEN_TAGS[t] for t in current) or "ничего"
    await cb.message.edit_text(
        f"Исключено: {excluded}\n\nДобавить ещё или завершить?",
        reply_markup=allergens_kb())
    await safe_answer(cb)


async def finalize(cb: CallbackQuery, state: FSMContext):
    user = await get_user(cb.from_user.id)
    data = await state.get_data()
    wants_full = bool(data.get("wants_full", False))
    exclude = set(data.get("allergens") or [])

    required = [data.get(k) for k in ("age", "height", "weight", "gender", "activity", "goal")]
    if any(v is None for v in required):
        await cb.message.edit_text("❌ Не все параметры заполнены. Попробуй начать заново через /start")
        await state.clear()
        await safe_answer(cb)
        return

    try:
        bmr = harris_benedict_bmr(data["gender"], data["weight"], data["height"], data["age"])
        tdee = calc_tdee(bmr, data["activity"])
        target = calc_target(tdee, data["goal"])
    except ValueError as e:
        await cb.message.edit_text(f"❌ {e}\nПопробуй пересчитать /start")
        await state.clear()
        await safe_answer(cb)
        return

    try:
        target = validate_target_kcal(target)
    except ValueError as e:
        await cb.message.edit_text(
            f"❌ {e}\n\nПопробуй изменить параметры расчёта или обратись в поддержку."
        )
        await state.clear()
        await safe_answer(cb)
        return

    # Сохраняем результат расчёта и только выбранные категории исключений.
    # Исходные параметры анкеты (возраст, рост, вес и т.д.) в БД не попадают.
    # Исключения нужны для восстановления требований к меню после перезапуска
    # или если оплата/выдача доступа завершается уже в другой сессии.
    await upsert_user(
        cb.from_user.id,
        target_kcal=target,
        exclude_tags_json=json.dumps(sorted(exclude), ensure_ascii=False),
    )

    if user and has_active_access(user):
        await cb.message.edit_text(
            f"📊 Расчёт готов: целевая калорийность {target:.0f} ккал/день")
        # Сразу генерируем план, пока исключения доступны в текущей сессии.
        await build_plan_and_store(cb.message, cb.from_user.id, target, exclude, new_purchase=False)
    elif wants_full:
        # До показа оплаты проверяем, что текущие исключения позволяют
        # построить полный 30-дневный план. Пользователь не должен оплатить
        # услугу, которая заведомо не может быть сгенерирована.
        try:
            build_plan(int(target), days=PLAN_DAYS, exclude_tags=exclude)
        except ValueError as e:
            await cb.message.edit_text(
                "⚠️ Сейчас не удалось составить полный 30-дневный план с такими исключениями.\n\n"
                f"{e}\n\n"
                "Убери одно или несколько исключений и нажми «Готово» ещё раз.",
                reply_markup=allergens_kb(),
            )
            await safe_answer(cb)
            return
        await mark_purchase_reminder_eligible(cb.from_user.id)
        await cb.message.edit_text(
            f"✅ Расчёт готов! Целевая калорийность: {target:.0f} ккал/день.\n\n"
            "В полный план входят: меню на 30 дней, граммовка ингредиентов, рецепты на каждый день, "
            "список покупок блоками по 5 дней и PDF.\n\n"
            "Исходные параметры анкеты не сохраняются в профиле.\n\n"
            "Если всё подходит — переходи к оплате:",
            reply_markup=pay_step_kb(PRICE_KOPECKS / 100, TEST_MODE))
    else:
        try:
            plan, shopping = build_plan(int(target), days=1, exclude_tags=exclude)
        except ValueError as e:
            await cb.message.edit_text(f"❌ {e}")
            await state.clear()
            await safe_answer(cb)
            return
        await upsert_user(cb.from_user.id,
                          demo_used=1,
                          plan_json=json.dumps(plan, ensure_ascii=False),
                          shopping_json=json.dumps(shopping, ensure_ascii=False))
        await cb.message.edit_text(
            f"🎁 Демо-расчёт готов!\n"
            f"📊 Целевая калорийность: {target:.0f} ккал/день\n\n"
            "Исходные параметры анкеты не сохраняются в профиле.\n\n"
            "Ниже — твой бесплатный план на 1 день 👇")
        await send_day(cb.message, plan[0], 1)
        await mark_purchase_reminder_eligible(cb.from_user.id)
        await cb.message.answer(
            "Понравилось? 😊\n"
            "Демо доступно только 1 раз. В полном плане на 30 дней: меню на каждый день "
            "с рецептами и граммовкой, список покупок, PDF и бесплатные пересчёты "
            "в течение срока действия.",
            reply_markup=after_demo_kb(PRICE_KOPECKS / 100))

    await state.clear()


@dp.callback_query(F.data == "show_plan")
async def show_plan(cb: CallbackQuery):
    await safe_answer(cb)
    user = await get_user(cb.from_user.id)
    if not user or not user.get("plan_json"):
        return
    if not can_view_plan(user):
        await cb.message.answer(
            "⏳ Срок полного доступа закончился. Оформи новый план на 30 дней.",
            reply_markup=pay_step_kb(PRICE_KOPECKS / 100, TEST_MODE))
        return

    try:
        plan = json.loads(user["plan_json"])
    except json.JSONDecodeError:
        await cb.message.answer("План повреждён. Напиши в поддержку.")
        return

    await cb.message.answer(
        "📅 <b>Твой план питания</b>\n\n"
        "Сначала выбери нужную неделю. Затем можно открыть любой день, "
        "рецепты или список покупок.\n\n"
        "Так удобнее, чем отправлять все 30 дней одним сообщением.",
        parse_mode="HTML",
        reply_markup=plan_overview_kb(len(plan)),
    )


@dp.callback_query(F.data.startswith("plan_week_"))
async def show_plan_week(cb: CallbackQuery):
    await safe_answer(cb)
    user = await get_user(cb.from_user.id)
    if not user or not user.get("plan_json"):
        return
    if not can_view_plan(user):
        await cb.message.answer("Срок доступа закончился. Откройте /start.")
        return

    try:
        plan = json.loads(user["plan_json"])
        start = int(cb.data.replace("plan_week_", ""))
        if not isinstance(plan, list) or start < 0 or start >= len(plan):
            raise IndexError
        end = min(start + 7, len(plan))
    except (ValueError, TypeError, IndexError, json.JSONDecodeError):
        await cb.message.answer("Неделя плана не найдена.")
        return

    lines = [f"📅 <b>Дни {start + 1}–{end}</b>\n"]
    for idx in range(start, end):
        day = plan[idx]
        total = day.get("total", {})
        lines.append(
            f"<b>День {idx + 1} · {day['date']}</b> — "
            f"{total.get('kcal', 0)} ккал\n"
            f"🌅 {day['meals'][0]['name']}\n"
            f"☀️ {day['meals'][2]['name']}\n"
            f"🌙 {day['meals'][4]['name']}\n"
        )

    await cb.message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=plan_week_kb(start, end),
    )


@dp.callback_query(F.data == "plan_today")
async def show_plan_today(cb: CallbackQuery):
    await safe_answer(cb)
    user = await get_user(cb.from_user.id)
    if not user or not user.get("plan_json"):
        return
    if not can_view_plan(user):
        await cb.message.answer("Срок доступа закончился. Откройте /start.")
        return

    try:
        plan = json.loads(user["plan_json"])
        today = service_today().strftime("%d.%m.%Y")
        index = next((i for i, day in enumerate(plan) if day.get("date") == today), 0)
    except (TypeError, json.JSONDecodeError):
        await cb.message.answer("План не найден.")
        return
    await send_day(cb.message, plan[index], index + 1, len(plan))


@dp.callback_query(F.data.startswith("plan_day_"))
async def show_plan_day(cb: CallbackQuery):
    await safe_answer(cb)
    user = await get_user(cb.from_user.id)
    if not user or not user.get("plan_json"):
        return
    if not can_view_plan(user):
        await cb.message.answer("Срок доступа закончился. Откройте /start.")
        return

    try:
        plan = json.loads(user["plan_json"])
        index = int(cb.data.replace("plan_day_", ""))
        if not isinstance(plan, list) or not (0 <= index < len(plan)):
            raise IndexError
    except (ValueError, TypeError, IndexError, json.JSONDecodeError):
        await cb.message.answer("День плана не найден.")
        return

    await send_day(cb.message, plan[index], index + 1, len(plan))


@dp.callback_query(F.data.startswith("day_recipes_"))
async def show_day_recipes(cb: CallbackQuery):
    await safe_answer(cb)
    try:
        day_index = int(cb.data.replace("day_recipes_", ""))
    except (ValueError, AttributeError):
        return

    user = await get_user(cb.from_user.id)
    if not user or not user.get("plan_json"):
        await cb.message.answer("План не найден.")
        return
    if not can_view_plan(user):
        await cb.message.answer("⏳ Срок полного доступа закончился.")
        return

    try:
        plan = json.loads(user["plan_json"])
        if not isinstance(plan, list) or not (0 <= day_index < len(plan)):
            raise IndexError
        day = plan[day_index]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        await cb.message.answer("Рецепты за этот день не найдены.")
        return

    labels = DAY_LABELS
    parts = [f"🍽 <b>Рецепты — День {day_index + 1} · {day['date']}</b>\n"]
    for j, meal in enumerate(day["meals"]):
        parts.append(
            f"<b>{labels[j]}: {meal['name']}</b>\n"
            f"📊 {meal['kcal']} ккал | Б:{meal['p']} Ж:{meal['f']} У:{meal['c']}\n"
            "📦 <b>Ингредиенты:</b>\n"
            + "".join(f"• {name} — {amount} {unit}\n" for name, amount, unit in meal["ingredients"])
            + f"👨‍🍳 <b>Приготовление:</b> {meal['recipe']}\n"
        )

    text = "\n".join(parts)
    # Обычно день помещается в одно сообщение. Защита оставлена на случай
    # длинных рецептов из будущего каталога.
    if len(text) <= 4000:
        await cb.message.answer(text, parse_mode="HTML")
        return

    chunk = ""
    for part in parts:
        if len(chunk) + len(part) > 3900 and chunk:
            await cb.message.answer(chunk, parse_mode="HTML")
            chunk = ""
        chunk += part
    if chunk:
        await cb.message.answer(chunk, parse_mode="HTML")


# Совместимость со старыми сообщениями, где ещё могли остаться кнопки recipe_* .
@dp.callback_query(F.data.startswith("recipe_"))
async def show_recipe_legacy(cb: CallbackQuery):
    await safe_answer(cb)
    try:
        _, di, mi = cb.data.split("_")
        di, mi = int(di), int(mi)
    except (ValueError, AttributeError):
        return
    user = await get_user(cb.from_user.id)
    if not user or not user.get("plan_json"):
        await cb.message.answer("План не найден.")
        return
    if not can_view_plan(user):
        await cb.message.answer("⏳ Срок полного доступа закончился.")
        return
    try:
        plan = json.loads(user["plan_json"])
        if not isinstance(plan, list) or not (0 <= di < len(plan)):
            raise IndexError
        meals = plan[di].get("meals", [])
        if not (0 <= mi < len(meals)):
            raise IndexError
        meal = meals[mi]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        await cb.message.answer("Рецепт не найден.")
        return
    ingredients_text = "📦 <b>Ингредиенты:</b>\n" + "".join(
        f"• {name} — {amount} {unit}\n" for name, amount, unit in meal["ingredients"]
    )
    await cb.message.answer(
        f"<b>{meal['name']}</b>\n"
        f"📊 {meal['kcal']} ккал | Б:{meal['p']} Ж:{meal['f']} У:{meal['c']}\n\n"
        f"{ingredients_text}\n"
        f"👨‍🍳 <b>Приготовление:</b>\n{meal['recipe']}",
        parse_mode="HTML")


@dp.callback_query(F.data == "show_shopping")
async def show_shopping(cb: CallbackQuery):
    await safe_answer(cb)
    user = await get_user(cb.from_user.id)
    if not user:
        return
    if not can_view_plan(user):
        await cb.message.answer("⏳ Срок полного доступа закончился.")
        return
    try:
        shopping = json.loads(user.get("shopping_json") or "[]")
    except json.JSONDecodeError:
        await cb.message.answer("Список покупок повреждён. Напиши в поддержку.")
        return
    if not shopping:
        await cb.message.answer("Список покупок не найден.")
        return

    # Новый формат: отдельная закупка на каждые 5 дней.
    if shopping and isinstance(shopping[0], dict):
        for group in shopping:
            text = (
                f"🛒 <b>Закупка: дни {group['days_start']}–{group['days_end']}</b>\n\n"
            )
            sections = group.get("sections") or {}
            if sections:
                for category, items in sections.items():
                    section = f"<b>{category}</b>\n"
                    for name, amount, unit in items:
                        section += f"• {name} — {amount} {unit}\n"
                    section += "\n"
                    if len(text) + len(section) > 3900 and text.strip():
                        await cb.message.answer(text, parse_mode="HTML")
                        text = ""
                    text += section
            else:
                for name, amount, unit in group["items"]:
                    line = f"• {name} — {amount} {unit}\n"
                    if len(text) + len(line) > 3900:
                        await cb.message.answer(text, parse_mode="HTML")
                        text = ""
                    text += line
            if text.strip():
                await cb.message.answer(text, parse_mode="HTML")
    else:
        # Совместимость со старыми сохранёнными планами.
        text = "🛒 <b>Список покупок (с учётом твоих порций):</b>\n\n"
        for name, amount, unit in shopping:
            line = f"• {name} — {amount} {unit}\n"
            if len(text) + len(line) > 4000:
                await cb.message.answer(text, parse_mode="HTML")
                text = ""
            text += line
        if text:
            await cb.message.answer(text, parse_mode="HTML")



@dp.callback_query(F.data == "download_pdf")
async def download_pdf(cb: CallbackQuery):
    await safe_answer(cb, "⏳ Генерирую PDF...")
    user = await get_user(cb.from_user.id)
    if not user or not user.get("plan_json"):
        await cb.message.answer("План не найден.")
        return
    if not has_active_access(user):
        await cb.message.answer("PDF доступен только при активном полном доступе.")
        return

    plans_dir = os.path.join(BASE_DIR, "plans")
    os.makedirs(plans_dir, exist_ok=True)
    fd, out_path = tempfile.mkstemp(prefix=".tmp_", suffix=".pdf", dir=plans_dir)
    os.close(fd)
    try:
        await asyncio.to_thread(generate_pdf, user, out_path)
        await cb.message.answer_document(
            FSInputFile(out_path, filename="my_plan.pdf"),
            caption="📄 Твой информационный план питания")
    except FileNotFoundError as e:
        await cb.message.answer(f"❌ {e}")
    except Exception:
        logging.warning("Ошибка генерации PDF")
        await cb.message.answer("❌ Ошибка генерации PDF. Попробуйте позже.")
    finally:
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except OSError:
            logging.warning("Не удалось удалить временный PDF")


@dp.callback_query(F.data == "recalc")
async def recalc(cb: CallbackQuery, state: FSMContext):
    await safe_answer(cb)
    user = await get_user(cb.from_user.id)
    if not user or not has_active_access(user):
        await cb.message.answer("Пересчёт доступен только при активном полном доступе.")
        return
    await state.clear()
    await state.update_data(wants_full=True)
    await cb.message.answer(
        "🔄 Пересчёт плана (на оставшиеся дни).\n\nВведи возраст:",
        reply_markup=main_menu_kb())
    await state.set_state(Onboarding.waiting_age)


def validate_runtime_config(webhook_mode: bool = False):
    from zoneinfo import ZoneInfo
    from config import SERVICE_TIMEZONE
    ZoneInfo(SERVICE_TIMEZONE)
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        raise RuntimeError("BOT_TOKEN не задан.")
    if not SUPPORT_URL.startswith("https://t.me/"):
        raise RuntimeError("SUPPORT_USERNAME задан некорректно.")
    if webhook_mode:
        if not WEBHOOK_URL.startswith("https://") or "yourdomain.com" in WEBHOOK_URL:
            raise RuntimeError("Для webhook укажи реальный WEBHOOK_URL с HTTPS.")
        if not WEBHOOK_SECRET_TOKEN or len(WEBHOOK_SECRET_TOKEN) < 16:
            raise RuntimeError("Для webhook укажи WEBHOOK_SECRET_TOKEN длиной не менее 16 символов.")

    # YooKassa checkout используется и в тестовом режиме. Не допускаем
    # ситуацию, когда кнопка оплаты есть, а ключи отсутствуют или перепутаны
    # между test/live контурами.
    from config import YOOKASSA_API_URL
    if YOOKASSA_API_URL.rstrip("/") != "https://api.yookassa.ru/v3":
        raise RuntimeError("Для платежей требуется официальный HTTPS API ЮKassa.")
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        raise RuntimeError("Для YooKassa необходимо указать YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY.")
    if not YOOKASSA_RETURN_URL.startswith("https://"):
        raise RuntimeError("YOOKASSA_RETURN_URL должен быть HTTPS URL.")
    if TEST_MODE and not YOOKASSA_SECRET_KEY.startswith("test_"):
        raise RuntimeError("TEST_MODE=True требует тестовый YooKassa secret key с префиксом test_.")
    if not TEST_MODE and YOOKASSA_SECRET_KEY.startswith("test_"):
        raise RuntimeError("TEST_MODE=False не может использовать тестовый YooKassa secret key.")

    if not TEST_MODE:
        if not ADMIN_USER_IDS:
            raise RuntimeError("Для production необходимо указать ADMIN_USER_IDS; username не используется как основной допуск.")
        if not PRIVACY_POLICY_URL.startswith("https://"):
            raise RuntimeError("Для production укажи публичную PRIVACY_POLICY_URL с HTTPS.")
        if not PUBLIC_OFFER_URL.startswith("https://"):
            raise RuntimeError("Для production укажи публичную PUBLIC_OFFER_URL с HTTPS.")
        if not OPERATOR_ADDRESS or OPERATOR_ADDRESS.startswith("["):
            raise RuntimeError("Не заполнен OPERATOR_ADDRESS.")
        if not OPERATOR_EMAIL or OPERATOR_EMAIL.startswith("["):
            raise RuntimeError("Не заполнен OPERATOR_EMAIL.")


async def on_startup(**_):
    global reminder_task, recovery_task
    validate_runtime_config(webhook_mode=True)
    info = await bot.get_webhook_info()
    expected = WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
    if info.url != expected:
        raise RuntimeError("Telegram webhook не соответствует этому приложению. Автоматическое переключение запрещено.")
    await init_db()
    await configure_bot_navigation()
    reminder_task = asyncio.create_task(purchase_reminder_loop())
    recovery_task = asyncio.create_task(payment_recovery_loop())
    logging.info("Configured webhook verified; no changes made")


async def on_shutdown(**_):
    global reminder_task, recovery_task
    if recovery_task:
        recovery_task.cancel()
        try: await recovery_task
        except asyncio.CancelledError: pass
        recovery_task = None
    if reminder_task:
        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass
        reminder_task = None
    await dp.storage.close()
    await bot.session.close()


async def yookassa_webhook(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        if payload.get("type") != "notification" or payload.get("event") != "payment.succeeded":
            return web.json_response({"ok": True})
        payment_id = (payload.get("object") or {}).get("id")
        if not payment_id:
            return web.json_response({"ok": False, "error": "missing_payment_id"}, status=400)
        await process_yookassa_success(str(payment_id))
        return web.json_response({"ok": True})
    except ValueError as exc:
        logging.warning("Rejected YooKassa webhook")
        return web.json_response({"ok": False, "error": "payment_rejected"}, status=400)
    except Exception:
        logging.warning("YooKassa webhook processing failed")
        return web.json_response({"ok": False, "error": "temporary_error"}, status=500)


async def payment_return(request: web.Request) -> web.Response:
    return web.Response(
        text="Оплата обрабатывается. Вернитесь в Telegram — бот автоматически выдаст доступ после подтверждения YooKassa.",
        content_type="text/plain",
        charset="utf-8",
    )


dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)


def create_web_app() -> web.Application:
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    app = web.Application(client_max_size=1024 * 1024)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET_TOKEN).register(app, path=WEBHOOK_PATH)
    app.router.add_post(YOOKASSA_WEBHOOK_PATH, yookassa_webhook)
    app.router.add_get("/payment/return", payment_return)
    app.router.add_get("/health", lambda request: web.json_response({"ok": True, "service": "nyambo"}))
    setup_application(app, dp, bot=bot)
    return app


async def start_polling():
    global reminder_task
    validate_runtime_config(webhook_mode=False)
    if (await bot.get_webhook_info()).url:
        raise RuntimeError("Polling запрещён при установленном webhook")
    await init_db()
    await configure_bot_navigation()
    reminder_task = asyncio.create_task(purchase_reminder_loop())
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass
        reminder_task = None


if __name__ == "__main__":
    if "--webhook" in sys.argv:
        web.run_app(create_web_app(), host="0.0.0.0", port=WEBHOOK_PORT)
    else:
        asyncio.run(start_polling())
from aiogram.fsm.state import State, StatesGroup


class Onboarding(StatesGroup):
    waiting_payment = State()
    waiting_age = State()
    waiting_height = State()
    waiting_weight = State()
    waiting_gender = State()
    waiting_activity = State()
    waiting_goal = State()
    waiting_allergens = State()


class Admin(StatesGroup):
    waiting_user_id = State()  # username или Telegram ID

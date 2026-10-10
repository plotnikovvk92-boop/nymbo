from collections import defaultdict
from datetime import date, timedelta
from access import service_today

from meals import MEALS
from config import ACTIVITY_FACTORS, GOALS

MIN_FACTOR = 0.5
MAX_FACTOR = 2.5
MAX_PIECES = {"яйца": 4, "яйцо": 4}

# Технический диапазон, в котором текущий каталог позволяет сформировать
# план без чрезмерного масштабирования отдельных блюд.
SUPPORTED_TARGET_MIN = 1100
SUPPORTED_TARGET_MAX = 3600

SLOTS = [
    ("breakfast", 0.25),
    ("snack", 0.10),
    ("lunch", 0.35),
    ("snack", 0.10),
    ("dinner", 0.20),
]

# Ротация настроена так, чтобы меню не ощущалось «одним и тем же»:
# перекусы повторяются реже остальных слотов, а между одинаковыми блюдами
# остаётся несколько дней.
CAPS = {"breakfast": 3, "snack": 3, "lunch": 2, "dinner": 2}
GAP = 6

# Внутренний, ориентировочный ценовой приоритет. Это НЕ пользовательский
# бюджет и не меняет формулу расчёта калорийности. Он лишь слегка помогает
# генератору чаще использовать доступную базу продуктов.
_CHEAP_PRODUCTS = {
    "овсяные хлопья", "гречка ядрица", "рис длиннозерный пропаренный", "перловая крупа", "пшено шлифованное", "макароны из твёрдых сортов пшеницы", "картофель",
    "капуста", "морковь", "лук", "свёкла", "яблоко", "банан", "хлеб цельнозерновой ржано-пшеничный",
    "чечевица зелёная сухая", "горох колотый сухой", "красная фасоль варёная", "нут варёный", "замороженная овощная смесь (брокколи, цветная капуста, морковь)",
    "филе куриной грудки", "филе куриной грудки", "масло подсолнечное рафинированное", "огурец", "помидор",
}
_PREMIUMER_PRODUCTS = {
    "филе грудки индейки", "тунец консервированный в собственном соку", "филе минтая", "черника замороженная",
    "грецкие орехи очищенные", "арахис жареный несолёный", "сыр полутвёрдый 45%", "творожный сыр сливочный 60%", "семечки подсолнечника очищенные",
}


def _meal_cost_score(meal: dict) -> float:
    """Грубая относительная оценка стоимости блюда для выбора.

    Это не расчёт цены и не обещание стоимости покупки: реальные цены
    зависят от магазина и региона. Используется только как слабый фактор
    разнообразия/доступности при равной близости к целевой калорийности.
    """
    score = 0.0
    for name, amount, _unit in meal.get("ingredients", []):
        key = str(name).strip().lower()
        if key in _PREMIUMER_PRODUCTS:
            score += 1.0
        elif key not in _CHEAP_PRODUCTS:
            score += 0.35
    return score / max(1, len(meal.get("ingredients", [])))


def _meal_ingredient_keys(meal: dict) -> set[str]:
    return {
        canonical_product_name(name)
        for name, _amount, _unit in meal.get("ingredients", [])
    }


# Единые названия продуктов для списка покупок.
# В меню блюда могут использовать грамматически разные формы (например,
# «яйцо» и «яйца»), но в закупке это должен быть один продукт.
PRODUCT_ALIASES = {
    "яйцо": "яйца",
    "яйца": "яйца",
    "грецкий орех": "грецкие орехи очищенные",
    "грецкие орехи очищенные": "грецкие орехи очищенные",
}


def canonical_product_name(name: str) -> str:
    key = " ".join(str(name).strip().lower().split())
    return PRODUCT_ALIASES.get(key, key)


def harris_benedict_bmr(gender: str, weight_kg: float, height_cm: float, age: int) -> float:
    if gender == "m":
        return 88.362 + (13.397 * weight_kg) + (4.799 * height_cm) - (5.677 * age)
    if gender == "f":
        return 447.593 + (9.247 * weight_kg) + (3.098 * height_cm) - (4.330 * age)
    raise ValueError("Некорректно указан вариант пола.")


def calc_tdee(bmr: float, activity_key: str) -> float:
    try:
        factor = ACTIVITY_FACTORS[activity_key][1]
    except KeyError as exc:
        raise ValueError("Некорректно указан уровень активности.") from exc
    return bmr * factor


def calc_target(tdee: float, goal_key: str) -> float:
    try:
        modifier = GOALS[goal_key][1]
    except KeyError as exc:
        raise ValueError("Некорректно указана цель.") from exc
    return tdee * (1 + modifier)


# Ингредиентная защита: теги блюда остаются основным быстрым фильтром,
# но дополнительно проверяем фактический состав блюда. Это не позволяет
# случайно пропустить запрещённый продукт при добавлении нового блюда.
INGREDIENT_EXCLUSION_TAGS = {
    "gluten": {
        "овсяные хлопья",
        "хлеб цельнозерновой ржано-пшеничный",
        "лаваш пшеничный тонкий",
        "макароны из твёрдых сортов пшеницы",
        "перловая крупа",
        "булгур крупный",
    },
    "dairy": {
        "молоко 2,5% пастеризованное",
        "творог 5%",
        "кефир 2,5%",
        "йогурт натуральный 2,5% без сахара",
        "творожный сыр сливочный 60%",
        "сыр полутвёрдый 45%",
    },
    "nuts": {
        "арахис жареный несолёный",
        "грецкие орехи очищенные",
    },
    "seeds": {
        "семечки подсолнечника очищенные",
    },
    "eggs": {
        "яйца",
        "яйцо",
    },
    "soy": set(),
    "fish": {
        "филе минтая",
        "тунец консервированный в собственном соку",
    },
    "meat": {
        "филе куриной грудки",
        "филе грудки индейки",
    },
    "honey": {
        "мёд",
        "мед",
    },
}


def meal_ingredient_tags(meal: dict) -> set:
    """Возвращает ограничения, найденные непосредственно в ингредиентах."""
    tags = set()
    ingredient_names = {str(item[0]).strip().casefold() for item in meal.get("ingredients", [])}
    for tag, products in INGREDIENT_EXCLUSION_TAGS.items():
        if ingredient_names & {p.casefold() for p in products}:
            tags.add(tag)
    return tags


def meal_exclusion_tags(meal: dict) -> set:
    """Объединяет явные теги блюда и ограничения из фактических ингредиентов."""
    return set(meal.get("tags", set())) | meal_ingredient_tags(meal)


def filter_meals(exclude_tags: set) -> list:
    if not exclude_tags:
        return MEALS
    excluded = set(exclude_tags)
    return [m for m in MEALS if not (meal_exclusion_tags(m) & excluded)]


def validate_plan_exclusions(plan: list, exclude_tags: set) -> None:
    """Финальная защита: в готовом плане не должно быть запрещённых ингредиентов."""
    excluded = set(exclude_tags or set())
    if not excluded:
        return
    violations = []
    for day in plan:
        for meal in day.get("meals", []):
            found = meal_exclusion_tags(meal) & excluded
            if found:
                violations.append(f"{day.get('day')}: {meal.get('name')}: {sorted(found)}")
    if violations:
        raise ValueError("В сгенерированном плане обнаружены исключённые продукты: " + "; ".join(violations[:5]))


def _clean_number(value: float):
    value = round(value, 1)
    if isinstance(value, int):
        return value
    return int(value) if value.is_integer() else value


def _round_to_step(value: float, step: float):
    return _clean_number(round(value / step) * step)


# Практичные шаги для выдачи граммовки.
# Важное правило: калорийность/БЖУ блюда считаются отдельно от округления
# ингредиентов, поэтому округление не меняет дневную калорийность плана.
_OIL_NAMES = {"масло подсолнечное рафинированное", "масло оливковое"}


def _round_ingredient_amount(name: str, amount: float, unit: str) -> float:
    name_l = name.lower()

    # Штуки для пользователя должны быть реальными штуками, а не 3.6 яйца.
    if unit == "шт":
        return int(max(1, round(amount)))

    if unit == "мл":
        return int(max(10, round(amount / 50) * 50))

    if unit != "г":
        return round(amount, 1)

    # Масло — шаг 5 г: 5, 10, 15...
    if name_l in _OIL_NAMES or "масло" in name_l:
        return int(max(5, round(amount / 5) * 5))

    # Мелкие добавки удобно считать по 5 г, основные продукты — по 10 г.
    if amount < 50:
        return _round_to_step(amount, 5)
    return _round_to_step(amount, 10)


def _meal_factor_cap(m: dict) -> float:
    """Максимальный коэффициент, при котором количество штучных продуктов остаётся практичным."""
    cap = MAX_FACTOR
    for name, amount, unit in m.get("ingredients", []):
        key = str(name).strip().lower()
        if unit == "шт" and key in MAX_PIECES and amount > 0:
            cap = min(cap, MAX_PIECES[key] / amount)
    return cap


def _scale_meal(m: dict, factor: float) -> dict:
    f = round(factor, 2)
    return {
        "name": m["name"],
        "factor": f,
        "max_factor": round(_meal_factor_cap(m), 2),
        "kcal": int(round(m["kcal"] * f)),
        "p": int(round(m["p"] * f)),
        "f": int(round(m["f"] * f)),
        "c": int(round(m["c"] * f)),
        "tags": sorted(m.get("tags", set())),
        "ingredients": [
            [name, _round_ingredient_amount(name, amount * f, unit), unit]
            for name, amount, unit in m["ingredients"]
        ],
        "recipe": m["recipe"],
    }


def _pick_meals_for_day(target_kcal, allowed_meals, used_counts, last_used,
                         day_idx, window_ingredients):
    """Выбирает блюда по калорийности, разнообразию и разумному переиспользованию продуктов.

    Формула расчёта target_kcal не меняется. Улучшается только подбор конкретных
    блюд: меньше повторов, умеренное переиспользование ингредиентов в одной
    закупке и слабый приоритет доступных продуктов.
    """
    day = []
    day_names = set()

    for slot, share in SLOTS:
        goal = target_kcal * share
        pool = [m for m in allowed_meals if m["slot"] == slot]

        c1 = [
            m for m in pool
            if m["name"] not in day_names
            and used_counts.get(m["name"], 0) < CAPS[slot]
            and day_idx - last_used.get(m["name"], -100) >= GAP
        ]
        c2 = [
            m for m in pool
            if m["name"] not in day_names
            and used_counts.get(m["name"], 0) < CAPS[slot]
        ]
        c3 = [m for m in pool if m["name"] not in day_names]
        # Сначала соблюдаем и лимит повторов, и минимальный разрыв.
        # Если это невозможно, ослабляем только разрыв, затем лимит повторов.
        # Полный fallback к pool допускается только для действительно малого
        # каталога, чтобы не получать одно и то же блюдо десятки раз без нужды.
        candidates = c1 or c2 or c3
        if not candidates:
            candidates = [m for m in pool if m["name"] not in day_names]
        if not candidates:
            candidates = pool

        def selection_score(m):
            calorie_error = abs(m["kcal"] - goal) / max(goal, 1)
            capacity_error = max(0.0, goal - (m["kcal"] * _meal_factor_cap(m))) / max(goal, 1)
            repeat_penalty = used_counts.get(m["name"], 0) * 0.08
            cost_penalty = _meal_cost_score(m) * 0.035
            ingredient_overlap = len(_meal_ingredient_keys(m) & window_ingredients)
            # Небольшой бонус за общие продукты внутри одного 5-дневного
            # закупочного окна: меньше открытых упаковок и пищевых отходов.
            reuse_bonus = min(ingredient_overlap, 4) * 0.018
            return calorie_error + capacity_error * 1.5 + repeat_penalty + cost_penalty - reuse_bonus

        best = min(candidates, key=selection_score)

        factor = min(_meal_factor_cap(best), max(MIN_FACTOR, goal / best["kcal"]))
        used_counts[best["name"]] = used_counts.get(best["name"], 0) + 1
        last_used[best["name"]] = day_idx
        day_names.add(best["name"])
        window_ingredients.update(_meal_ingredient_keys(best))
        day.append(_scale_meal(best, factor))

    # После выбора блюд используем свободную ёмкость уже выбранных блюд,
    # если из-за верхнего коэффициента отдельного блюда день недобрал цель.
    # Это не меняет формулу расчёта target_kcal.
    deficit = int(round(target_kcal - sum(m["kcal"] for m in day)))
    if deficit > 0:
        source_by_name = {m["name"]: m for m in allowed_meals}
        ranked = sorted(
            range(len(day)),
            key=lambda i: (
                day[i]["factor"] < day[i].get("max_factor", MAX_FACTOR),
                not any(unit == "шт" for _n, _a, unit in day[i]["ingredients"]),
                day[i]["kcal"],
            ),
            reverse=True,
        )
        for idx in ranked:
            if deficit <= 0:
                break
            source = source_by_name.get(day[idx]["name"])
            if source is None:
                continue
            old_factor = float(day[idx]["factor"])
            base_kcal = max(1, int(round(source["kcal"])))
            meal_cap = float(_meal_factor_cap(source))
            max_add = int(round((meal_cap - old_factor) * base_kcal))
            if max_add <= 0:
                continue
            add = min(deficit, max_add)
            new_factor = round(min(meal_cap, old_factor + add / base_kcal), 2)
            day[idx] = _scale_meal(source, new_factor)
            deficit = int(round(target_kcal - sum(m["kcal"] for m in day)))

    return day


def validate_target_kcal(target_kcal: float) -> float:
    if not isinstance(target_kcal, (int, float)):
        raise ValueError("Целевая калорийность рассчитана некорректно.")
    value = float(target_kcal)
    if not (SUPPORTED_TARGET_MIN <= value <= SUPPORTED_TARGET_MAX):
        raise ValueError(
            f"Текущий каталог меню поддерживает расчёт примерно в диапазоне "
            f"{SUPPORTED_TARGET_MIN}–{SUPPORTED_TARGET_MAX} ккал/день."
        )
    return value


_SHOPPING_CATEGORIES = (
    ("🥬 Овощи и фрукты", ("яблоко", "банан", "апельсин", "мандарины", "груша",
                           "брокколи", "капуста", "картофель", "морковь", "огурец",
                           "помидор", "свёкла", "перец", "лук", "кабачок",
                           "замороженная овощная смесь (брокколи, цветная капуста, морковь)", "черника замороженная")),
    ("🥩 Мясо, рыба и яйца", ("филе куриной грудки", "филе грудки индейки", "филе минтая", "минтай", "хек", "тунец", "яйца", "яйцо")),
    ("🥛 Молочные продукты", ("молоко 2,5% пастеризованное", "кефир 2,5%", "йогурт натуральный 2,5% без сахара", "творог 5%", "творожный сыр сливочный 60%", "сыр полутвёрдый 45%")),
    ("🌾 Крупы, макароны и хлеб", ("овсяные хлопья", "гречка ядрица", "рис длиннозерный пропаренный", "перловая крупа", "пшено шлифованное",
                                   "булгур", "макароны из твёрдых сортов пшеницы", "хлеб цельнозерновой ржано-пшеничный", "лаваш пшеничный тонкий")),
    ("🫘 Бобовые и консервы", ("чечевица зелёная сухая", "горох колотый сухой", "фасоль", "нут варёный")),
    ("🥜 Орехи", ("арахис жареный несолёный", "грецкие орехи очищенные")),
    ("🌻 Семечки", ("семечки подсолнечника очищенные",)),
    ("🫙 Масла и прочее", ("масло",)),
)


def shopping_category(name: str) -> str:
    key = str(name).strip().lower()
    for category, keywords in _SHOPPING_CATEGORIES:
        if any(keyword in key for keyword in keywords):
            return category
    return "🧂 Остальное"



def build_plan(target_kcal: int, days: int = 30, exclude_tags: set | None = None,
                start_date=None):
    """Строит план на N дней, начиная с start_date (по умолчанию — сегодня)."""
    if not isinstance(days, int) or not (1 <= days <= 30):
        raise ValueError("Количество дней должно быть от 1 до 30.")
    target_kcal = validate_target_kcal(target_kcal)

    allowed = filter_meals(exclude_tags or set())
    if len(allowed) < 15:
        raise ValueError("Слишком мало блюд после фильтрации выбранных продуктов.")
    for slot, _ in SLOTS:
        if not any(m["slot"] == slot for m in allowed):
            raise ValueError("После выбранных исключений не осталось блюд для всех приёмов пищи.")

    used_counts = {}
    last_used = {}
    plan = []
    shopping_groups = []
    start = start_date or service_today()

    for i in range(days):
        # Переиспользование ингредиентов считается отдельно внутри каждого
        # 5-дневного окна закупки.
        window_ingredients = set()
        day_meals = _pick_meals_for_day(
            target_kcal, allowed, used_counts, last_used, i, window_ingredients
        )
        plan.append({
            "date": (start + timedelta(days=i)).strftime("%d.%m.%Y"),
            "meals": day_meals,
            "total": {
                "kcal": sum(m["kcal"] for m in day_meals),
                "p": sum(m["p"] for m in day_meals),
                "f": sum(m["f"] for m in day_meals),
                "c": sum(m["c"] for m in day_meals),
            }
        })

    # Закупки разбиты на практичные блоки по 5 дней.
    for group_start in range(0, days, 5):
        group_end = min(group_start + 5, days)
        by_product = defaultdict(lambda: defaultdict(float))
        for day in plan[group_start:group_end]:
            for meal in day["meals"]:
                for name, amount, unit in meal["ingredients"]:
                    canonical_name = canonical_product_name(name)
                    by_product[canonical_name][unit] += amount

        items = []
        for name, units in by_product.items():
            for unit, amount in units.items():
                items.append((name, round(amount, 1), unit))
        items.sort(key=lambda x: (shopping_category(x[0]), x[0]))
        sections = defaultdict(list)
        for item in items:
            sections[shopping_category(item[0])].append(item)
        shopping_groups.append({
            "days_start": group_start + 1,
            "days_end": group_end,
            "items": items,
            "sections": dict(sorted(sections.items(), key=lambda pair: pair[0])),
        })

    validate_plan_exclusions(plan, exclude_tags or set())
    return plan, shopping_groups

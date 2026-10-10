from pathlib import Path
import sys, unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"gymochka_app"))
from nutrition import MEALS, build_plan, canonical_product_name, meal_ingredient_tags, meal_exclusion_tags, validate_target_kcal
class NutritionCoreTests(unittest.TestCase):

    def test_product_aliases(self):
        self.assertEqual(canonical_product_name('яйцо'), 'яйца')
        self.assertEqual(canonical_product_name('  ЯЙЦА '), 'яйца')

    def test_plan_has_five_meals_and_six_shopping_groups(self):
        plan, shopping = build_plan(1800, days=30)
        self.assertEqual(len(plan), 30)
        self.assertEqual(len(shopping), 6)
        self.assertTrue(all((len(day['meals']) == 5 for day in plan)))

    def test_all_single_exclusions_build(self):
        tags = {'gluten', 'dairy', 'nuts', 'seeds', 'eggs', 'soy', 'fish', 'meat', 'honey'}
        for tag in tags:
            plan, _ = build_plan(1800, days=30, exclude_tags={tag})
            self.assertEqual(len(plan), 30)

    def test_exclusions_are_absent_from_plan_and_shopping(self):
        tags = {'gluten', 'dairy', 'nuts', 'seeds', 'eggs', 'soy', 'fish', 'meat', 'honey'}
        for tag in tags:
            plan, shopping = build_plan(1800, days=30, exclude_tags={tag})
            for day in plan:
                for meal in day['meals']:
                    self.assertFalse(meal_exclusion_tags(meal) & {tag}, f"{tag} попал в {meal['name']}")
            for group in shopping:
                for name, _, _ in group['items']:
                    fake = {'ingredients': [(name, 1, 'шт')], 'tags': set()}
                    self.assertFalse(meal_exclusion_tags(fake) & {tag}, f'{tag} попал в покупки: {name}')

    def test_all_explicit_meal_tags_cover_detectable_ingredients(self):
        for meal in MEALS:
            inferred = meal_ingredient_tags(meal)
            self.assertTrue(inferred <= set(meal.get('tags', set())), f"Не хватает тегов у блюда: {meal['name']} -> {inferred - set(meal.get('tags', set()))}")

    def test_ingredient_guard_catches_missing_manual_tag(self):
        fake_meal = {'name': 'Тестовое блюдо', 'tags': set(), 'ingredients': [('филе куриной грудки', 150, 'г')]}
        self.assertIn('meat', meal_exclusion_tags(fake_meal))

    def test_egg_aliases_are_merged(self):
        _, shopping = build_plan(1800, days=5)
        eggs = [item for item in shopping[0]['items'] if item[0] == 'яйца' and item[2] == 'шт']
        self.assertEqual(len(eggs), 1)

    def test_target_boundaries(self):
        validate_target_kcal(1100)
        validate_target_kcal(3600)
        with self.assertRaises(ValueError):
            validate_target_kcal(1099)
        with self.assertRaises(ValueError):
            validate_target_kcal(3601)

    def test_menu_variety_and_shopping_sections(self):
        plan, shopping = build_plan(1800, days=30)
        all_names = [meal['name'] for day in plan for meal in day['meals']]
        self.assertGreaterEqual(len(set(all_names)), 70)
        for meal_index in (1, 3):
            counts = {}
            for day in plan:
                name = day['meals'][meal_index]['name']
                counts[name] = counts.get(name, 0) + 1
            self.assertLessEqual(max(counts.values()), 3)
        self.assertTrue(all(('sections' in group for group in shopping)))
        self.assertTrue(all((group['sections'] for group in shopping)))

class QualityRegressionTests(unittest.TestCase):

    def test_high_calorie_range_hits_target_without_more_than_four_eggs(self):
        for target in (3400, 3600):
            plan, _ = build_plan(target, days=30)
            totals = [day['total']['kcal'] for day in plan]
            self.assertGreaterEqual(min(totals), target - 5)
            self.assertLessEqual(max(totals), target + 10)
            for day in plan:
                for meal in day['meals']:
                    for name, amount, unit in meal['ingredients']:
                        if unit == 'шт' and name in {'яйца', 'яйцо'}:
                            self.assertLessEqual(amount, 4, meal['name'])

    def test_shopping_categories_use_specific_names(self):
        from nutrition import shopping_category
        self.assertEqual(shopping_category('филе минтая'), '🥩 Мясо, рыба и яйца')
        self.assertEqual(shopping_category('яйца'), '🥩 Мясо, рыба и яйца')
        self.assertEqual(shopping_category('семечки подсолнечника очищенные'), '🌻 Семечки')
        self.assertEqual(shopping_category('грецкие орехи очищенные'), '🥜 Орехи')

    def test_seeds_are_not_classified_as_nuts(self):
        fake = {'ingredients': [('семечки подсолнечника очищенные', 10, 'г')], 'tags': set()}
        self.assertIn('seeds', meal_exclusion_tags(fake))
        self.assertNotIn('nuts', meal_exclusion_tags(fake))

    def test_all_exclusion_combinations_have_no_violations_when_buildable(self):
        import itertools
        tags = ('gluten', 'dairy', 'nuts', 'seeds', 'eggs', 'soy', 'fish', 'meat', 'honey')
        for size in range(1, len(tags) + 1):
            for combo in itertools.combinations(tags, size):
                excluded = set(combo)
                try:
                    plan, _ = build_plan(1800, days=5, exclude_tags=excluded)
                except ValueError:
                    continue
                for day in plan:
                    for meal in day['meals']:
                        self.assertFalse(meal_exclusion_tags(meal) & excluded, (combo, meal['name']))

class ExclusionPersistenceRegressionTests(unittest.TestCase):

    def test_exclusions_survive_rebuild_contract(self):
        import json
        from nutrition import build_plan, meal_exclusion_tags
        excluded = {'eggs'}
        saved = json.dumps(sorted(excluded), ensure_ascii=False)
        restored = set(json.loads(saved))
        plan, _ = build_plan(1800, days=30, exclude_tags=restored)
        for day in plan:
            for meal in day['meals']:
                self.assertFalse(meal_exclusion_tags(meal) & excluded, (day['date'], meal['name']))

    def test_every_single_exclusion_category_on_30_day_plan(self):
        from config import ALLERGEN_TAGS
        from nutrition import build_plan, meal_exclusion_tags
        for tag in ALLERGEN_TAGS:
            plan, _ = build_plan(1800, days=30, exclude_tags={tag})
            for day in plan:
                for meal in day['meals']:
                    self.assertFalse(meal_exclusion_tags(meal) & {tag}, (tag, day['date'], meal['name']))

if __name__ == "__main__": unittest.main()

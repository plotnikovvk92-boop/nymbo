# Маршрутизация через gymochka.ru

Это подготовленный вариант, ещё не установленный на Beget. Проверки локальные.

| Назначение | Каталог | Telegram webhook | ЮKassa webhook |
|---|---|---|---|
| Тест @K4gymtestochka_bot | /home/p/plotnixu/plotnixu.beget.tech/gymochka_app | https://gymochka.ru/webhook | https://gymochka.ru/yookassa/webhook |
| Production @nyambo_bot | /home/p/plotnixu/plotnixu.beget.tech/nyambo_app | https://gymochka.ru/production/webhook | https://gymochka.ru/production/yookassa/webhook |

Базы остаются bot.db в соответствующих каталогах. Возврат обоих магазинов:
https://gymochka.ru/payment/return. Сам возврат не выдаёт доступ.
Поддомен не используется и не требует сертификата для этой схемы.

Passenger загружает gateway как passenger_wsgi.py. Рядом должны лежать
passenger_gateway.py (тот же код) и passenger_app_bridge.py. Входной gateway
запускает по требованию отдельный Python-процесс каждого приложения.
Одинаковые имена модулей и окружение из .env изолированы. Обработка запросов
в пределах каждого процесса последовательная; фоновые задачи используют
event loop bridge. Несколько процессов Passenger используют SQLite FSM и
атомарные операции платежей. Polling и установка webhook при запуске отсутствуют.

До установки production обязательны:

- отдельный .env production: TEST_MODE=False, числовые ADMIN_USER_IDS,
  рабочие shop ID и secret key, WEBHOOK_PATH=/production/webhook,
  YOOKASSA_WEBHOOK_PATH=/production/yookassa/webhook;
- сохранение production BOT_TOKEN и DB_PATH; никаких копий тестовой базы/.env;
- актуальные документы и процедура выдачи чеков НПД через «Мой налог»;
- резервная копия кода, входного файла, .htaccess и обоих .env;
  SQLite через backup API, включая данные WAL;
- проверка gateway с временными базами на Beget и подтверждение допустимости
  дочерних процессов в Passenger на этом тарифе;
- отдельная установка webhook production с его секретом, без изменения тестового;
- подготовленный откат gateway к прежнему входному файлу с сохранением живых баз.

Не заменяйте входной файл вручную по этой памятке: сначала нужны серверная
проверка и подготовленная команда установки с резервной копией и откатом.
HTTP 200 /health относится к тесту; /production/health проверяет production.
Отсутствие ошибки одного приложения не подтверждает готовность другого.

Гарантированная работа напоминаний при завершении Passenger по бездействию
остаётся отдельной серверной проверкой. Эта схема сама не создаёт cron/daemon.

Финальная проверка: реальная оплата 89 ₽ пользователем → автоматический
доступ на 30 дней → актуальное меню → рецепты → покупки → PDF.

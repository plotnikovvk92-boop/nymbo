"""Offline regression: temporary SQLite, fake Telegram, local HTTP provider only."""
import asyncio
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'gymochka_app'))
os.environ.update(BOT_TOKEN='123456:LOCAL_TEST_ONLY_TOKEN_ABCDEFGHIJKLMNOPQRSTUVWXYZ', TEST_MODE='False',
    YOOKASSA_SHOP_ID='local-shop', YOOKASSA_SECRET_KEY='local-test-secret',
    YOOKASSA_RETURN_URL='https://local.example/payment/return', WEBHOOK_URL='https://local.example',
    WEBHOOK_SECRET_TOKEN='local-test-webhook-secret', ADMIN_USER_IDS='9001', SERVICE_TIMEZONE='Europe/Samara')
import config, database as db, bot as app, payment_service as ps, nutrition as n, keyboards as kb, sqlite_fsm
from access import active_access, service_today
from aiogram.fsm.storage.base import StorageKey
from aiogram.client.session.base import BaseSession
from aiogram.types import Message, Chat, WebhookInfo
from aiogram.exceptions import TelegramNetworkError


def payment(user=101, pid='payment-a', status='succeeded', **kw):
    p={'id':pid,'status':status,'paid':status=='succeeded','test':False,
       'recipient':{'account_id':'local-shop'},'amount':{'value':'89.00','currency':'RUB'},
       'metadata':{'user_id':str(user),'offer_version':config.PUBLIC_OFFER_VERSION},
       'confirmation':{'confirmation_url':'https://payment.example/redirect'}}
    p.update(kw); return p


def callback(user=101, data='pay_now'):
    return SimpleNamespace(from_user=SimpleNamespace(id=user,username=None),data=data,answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock(),edit_reply_markup=AsyncMock(),answer_document=AsyncMock()))


class FakeSession(BaseSession):
    def __init__(self): super().__init__(); self.calls=[]
    async def close(self): pass
    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if method.__api_method__=='getWebhookInfo': return WebhookInfo(url=config.WEBHOOK_URL+'/webhook', has_custom_certificate=False,pending_update_count=0)
        if method.__api_method__ in ('sendMessage','editMessageText','sendDocument'):
            return Message(message_id=len(self.calls),date=datetime.now(timezone.utc),chat=Chat(id=int(method.chat_id),type='private'),text=getattr(method,'text',None)).as_(bot)
        return True
    async def stream_content(self, *args, **kwargs):
        if False: yield b''


class DBCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=str(Path(self.tmp.name)/'test.db')
        self.patches=[patch.object(db,'DB_PATH',self.path),patch.object(sqlite_fsm,'DB_PATH',self.path)]
        for p in self.patches:p.start()
        await db.init_db()
        self.send=AsyncMock(); self.sendpatch=patch.object(app.bot,'send_message',self.send);self.sendpatch.start()
        await db.upsert_user(101,target_kcal=1800,consent_at='now',consent_version=config.CONSENT_VERSION, exclude_tags_json='[]')
    async def asyncTearDown(self):
        self.sendpatch.stop()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    async def bind(self,user=101,pid='payment-a'):
        intent=await db.get_or_create_yookassa_intent(user)
        await db.set_yookassa_intent_payment(user,pid,intent['idempotence_key'])
        return intent
    async def process(self,p=None):
        p=p or payment()
        with patch.object(app,'get_yookassa_payment',AsyncMock(return_value=p)):
            return await app.process_yookassa_success(p['id'])

    async def test_parallel_replay_single_credit_and_message(self):
        await self.bind()
        await asyncio.gather(*[self.process() for _ in range(8)])
        user=await db.get_user(101)
        self.assertTrue(active_access(user)); self.assertEqual(len(json.loads(user['plan_json'])),30)
        self.assertEqual(self.send.await_count,1)
        async with db._db() as c:
            self.assertEqual((await (await c.execute('SELECT count(*) FROM payments')).fetchone())[0],1)

    async def test_duplicate_after_expiry_does_not_extend(self):
        await self.bind();await self.process()
        old=(service_today()-timedelta(days=35)).isoformat()
        await db.upsert_user(101,purchase_date=old)
        self.assertFalse(await self.process())
        user=await db.get_user(101); self.assertEqual(user['purchase_date'],old);self.assertFalse(active_access(user));self.assertEqual(self.send.await_count,1)

    async def test_duplicate_after_manual_revoke_no_resurrection(self):
        await self.bind();await self.process();created=(await db.get_user(101))['created_at']
        await db.revoke_access(101);await self.process()
        u=await db.get_user(101);self.assertEqual(u['paid'],0);self.assertIsNone(u['plan_json']);self.assertEqual(u['created_at'],created)

    async def test_new_purchase_clears_demo_and_old_menu(self):
        await db.upsert_user(101,demo_used=1,plan_json='[{"old":true}]',shopping_json='["old"]',purchase_date=(service_today()-timedelta(days=40)).isoformat())
        await self.bind();await self.process()
        u=await db.get_user(101);plan=json.loads(u['plan_json'])
        self.assertEqual(len(plan),30);self.assertEqual(plan[0]['date'],service_today().strftime('%d.%m.%Y'));self.assertNotIn('old',plan[0])
        self.assertEqual(len(json.loads(u['shopping_json'])),6)

    async def test_repurchase_after_expiry_new_id_current_menu(self):
        await self.bind();await self.process()
        await db.upsert_user(101,purchase_date=(service_today()-timedelta(days=31)).isoformat())
        await self.bind(pid='payment-b');await self.process(payment(pid='payment-b'))
        u=await db.get_user(101);self.assertEqual(u['last_payment_id'],'payment-b');self.assertEqual(len(json.loads(u['plan_json'])),30)
        await self.process();self.assertEqual((await db.get_user(101))['last_payment_id'],'payment-b')

    async def test_duplicate_after_delete_and_reregistration(self):
        await self.bind();await self.process();await db.delete_user_data(101)
        self.assertFalse(await self.process());self.assertIsNone(await db.get_user(101));self.assertIsNone(await db.get_yookassa_intent(101))
        await db.upsert_user(101,consent_at='new',consent_version=config.CONSENT_VERSION)
        await self.process();self.assertFalse(active_access(await db.get_user(101)))

    async def test_unprocessed_old_payment_after_revoke_rejected(self):
        await self.bind();await db.revoke_access(101)
        with self.assertRaises(ValueError):await self.process()
        self.assertEqual((await db.get_user(101))["paid"],0)

    async def test_unprocessed_old_payment_after_manual_grant_rejected(self):
        await self.bind();await db.revoke_access_for_grant(101,service_today().isoformat())
        with self.assertRaises(ValueError):await self.process()
        self.assertIsNone((await db.get_user(101))["last_payment_id"])

    async def test_same_payment_wrong_owner_rejected(self):
        await self.bind();await self.process()
        with self.assertRaises(ValueError):await db.complete_yookassa_payment(202,'payment-a',service_today().isoformat(),'now','v')

    async def test_atomic_credit_rolls_back_on_sql_failure(self):
        await self.bind()
        async with db._db() as c:
            await c.execute("CREATE TRIGGER fail_credit BEFORE UPDATE OF paid ON users BEGIN SELECT RAISE(ABORT,'synthetic failure'); END");await c.commit()
        with self.assertRaises(Exception):await db.complete_yookassa_payment(101,'payment-a',service_today().isoformat(),'now','v')
        self.assertIsNone(await db.get_payment('payment-a'));self.assertEqual((await db.get_user(101))['paid'],0)

    async def test_plan_failure_preserves_access_then_recovers(self):
        await self.bind()
        with patch.object(app,'build_plan',side_effect=ValueError('impossible')):
            with self.assertRaises(RuntimeError):await self.process()
        u=await db.get_user(101);self.assertTrue(active_access(u));self.assertIsNone(u['plan_json']);initial_date=u['purchase_date']
        await self.process();self.assertIsNotNone((await db.get_user(101))['plan_json']);self.assertEqual((await db.get_user(101))['purchase_date'],initial_date)

    async def test_message_failure_recovers_without_credit_repeat(self):
        await self.bind();self.send.side_effect=RuntimeError('definite local failure')
        with self.assertRaises(RuntimeError):await self.process()
        u=await db.get_user(101);self.assertTrue(active_access(u));self.assertIsNotNone(u['plan_json'])
        self.send.side_effect=None;await self.process();await self.process();self.assertEqual(self.send.await_count,2)

    async def test_ambiguous_telegram_timeout_no_auto_duplicate(self):
        from aiogram.methods import SendMessage
        await self.bind();self.send.side_effect=TelegramNetworkError(method=SendMessage(chat_id=101,text='x'),message='ambiguous')
        await self.process();self.send.side_effect=None;await self.process()
        self.assertEqual(self.send.await_count,1);self.assertTrue(active_access(await db.get_user(101)))

    async def test_pending_cancel_no_credit(self):
        await self.bind()
        for status in ('pending','waiting_for_capture','canceled'):
            with self.subTest(status=status),self.assertRaises(ValueError):await self.process(payment(status=status))
        self.assertEqual((await db.get_user(101))['paid'],0)

    async def test_unknown_order_not_credited(self):
        with self.assertRaises(ValueError):await self.process()
        self.assertIsNone(await db.get_payment('payment-a'))

    async def test_timeout_lost_response_webhook_order_key(self):
        intent=await db.get_or_create_yookassa_intent(101)
        p=payment();p['metadata']['order_key']=intent['idempotence_key']
        await self.process(p);self.assertTrue(active_access(await db.get_user(101)))

    async def test_wrong_order_key_rejected(self):
        await db.get_or_create_yookassa_intent(101)
        p=payment();p['metadata']['order_key']='wrong'
        with self.assertRaises(ValueError):await self.process(p)

    async def test_known_checkout_get_only(self):
        await self.bind();cb=callback()
        with patch.object(app,'get_yookassa_payment',AsyncMock(return_value=payment(status='pending'))) as get,patch.object(app,'create_yookassa_payment',AsyncMock()) as post:
            await app.pay_now(cb);await app.pay_now(cb)
            self.assertEqual(get.await_count,2);post.assert_not_awaited()

    async def test_cancel_then_new_checkout_key(self):
        old=await self.bind();cb=callback()
        with patch.object(app,'get_yookassa_payment',AsyncMock(return_value=payment(status='canceled'))):await app.pay_now(cb)
        self.assertEqual((await db.get_yookassa_intent(101))['status'],'canceled')
        with patch.object(app,'create_yookassa_payment',AsyncMock(return_value=payment(pid='payment-b',status='pending'))) as post:
            await app.pay_now(cb)
            self.assertNotEqual(post.await_args.args[2],old['idempotence_key'])

    async def test_timeout_retry_same_key(self):
        cb=callback()
        with patch.object(app,'create_yookassa_payment',AsyncMock(side_effect=[TimeoutError(),payment(status='pending')])) as post:
            await app.pay_now(cb);await app.pay_now(cb)
            self.assertEqual(post.await_args_list[0].args[2],post.await_args_list[1].args[2])

    async def test_old_unknown_checkout_blocks_post_after_24h(self):
        await db.get_or_create_yookassa_intent(101)
        async with db._db() as c:
            await c.execute("UPDATE payment_intents SET created_at='2020-01-01 00:00:00'");await c.commit()
        with patch.object(app,'create_yookassa_payment',AsyncMock()) as post:await app.pay_now(callback());post.assert_not_awaited()

    async def test_lost_response_replay_payload_survives_config_change(self):
        first=await db.get_or_create_yookassa_intent(101)
        with patch.object(ps,"PUBLIC_OFFER_VERSION","changed"),patch.object(config,"YOOKASSA_RETURN_URL","https://changed.example/return"):
            second=await db.get_or_create_yookassa_intent(101)
        self.assertEqual(first["request"],second["request"])
        cb=callback()
        with patch.object(app,"create_yookassa_payment",AsyncMock(return_value=payment(status="pending"))) as post:
            await app.pay_now(cb)
            self.assertEqual(post.await_args.kwargs["payload"],first["request"])

    async def test_parallel_intent_same_key(self):
        intents=await asyncio.gather(*[db.get_or_create_yookassa_intent(101) for _ in range(10)])
        self.assertEqual(len({x['idempotence_key'] for x in intents}),1)

    async def test_active_access_blocks_checkout(self):
        await self.bind();await self.process()
        with self.assertRaises(ValueError):await db.get_or_create_yookassa_intent(101)

    async def test_recalc_invalidates_old_plan_and_cas(self):
        await self.bind();await self.process();u=await db.get_user(101)
        await db.upsert_user(101,target_kcal=2200,exclude_tags_json='["eggs"]')
        newer=await db.get_user(101);self.assertIsNone(newer['plan_json']);self.assertIsNone(newer['shopping_json'])
        self.assertFalse(await db.save_plan_if_current(101,u['plan_revision'],'old','old'))
        await app.build_and_send_full_plan(callback().message,101)
        plan=json.loads((await db.get_user(101))['plan_json']);n.validate_plan_exclusions(plan,{'eggs'})
        self.assertNotEqual(json.loads(u['plan_json']),plan)

    async def test_revocation_blocks_inflight_plan_save(self):
        await self.bind();await self.process();u=await db.get_user(101)
        await db.revoke_access(101)
        self.assertFalse(await db.save_plan_if_current(101,u['plan_revision'],'old','old'))

    async def test_cross_user_isolation(self):
        await db.upsert_user(202,target_kcal=2400,exclude_tags_json='["eggs"]')
        await self.bind();await self.process()
        self.assertEqual((await db.get_user(202))['paid'],0);self.assertIsNone((await db.get_user(202))['plan_json'])

    async def test_manual_grant_revoke_created_at_preserved(self):
        cb=callback(9001,'admin_grant_confirm:101');created=(await db.get_user(101))['created_at']
        await app.admin_grant_confirm(cb)
        self.assertTrue(active_access(await db.get_user(101)));self.assertEqual((await db.get_user(101))['created_at'],created)
        cb.data='admin_revoke_confirm:101';await app.admin_revoke_confirm(cb)
        self.assertEqual((await db.get_user(101))['paid'],0);self.assertEqual((await db.get_user(101))['created_at'],created)

    async def test_unauthorized_admin_buttons_no_change(self):
        for fn,data in ((app.admin_grant_confirm,'admin_grant_confirm:101'),(app.admin_revoke_confirm,'admin_revoke_confirm:101')):
            await fn(callback(333,data))
        self.assertEqual((await db.get_user(101))['paid'],0)
        self.assertFalse(app.is_admin(SimpleNamespace(id=333,username='gymochka_admin')))

    async def test_live_skip_denied_test_skip_preserved(self):
        state=SimpleNamespace(clear=AsyncMock());cb=callback()
        await app.skip_pay(cb,state);self.assertEqual((await db.get_user(101))['paid'],0)
        with patch.object(app,'TEST_MODE',True):await app.skip_pay(cb,state)
        self.assertTrue(active_access(await db.get_user(101)));self.assertEqual(len(json.loads((await db.get_user(101))['plan_json'])),30)
        def buttons(test):return [b.callback_data for row in kb.pay_step_kb(89,test).inline_keyboard for b in row]
        self.assertNotIn('skip_pay',buttons(False));self.assertIn('skip_pay',buttons(True))

    async def test_reminder_cutoff_claim_once_and_fresh_activity(self):
        now=datetime.now(timezone.utc);cutoff=(now-timedelta(hours=2)).isoformat(timespec='seconds');old=(now-timedelta(hours=3)).isoformat(timespec='seconds')
        await db.upsert_user(101,marketing_consent=1,last_activity_at=old,purchase_reminder_eligible_at=old)
        self.assertEqual(len(await db.get_due_purchase_reminders(cutoff)),1)
        await db.touch_activity(101,now.isoformat(timespec='seconds'))
        self.assertFalse(await db.claim_purchase_reminder(101,now.isoformat(),cutoff))
        await db.touch_activity(101,old)
        claims=await asyncio.gather(*[db.claim_purchase_reminder(101,now.isoformat(),cutoff) for _ in range(8)])
        self.assertEqual(sum(claims),1);self.assertEqual(await db.get_due_purchase_reminders(cutoff),[])

    async def test_no_reminder_after_payment_grant_or_optout(self):
        old='2020-01-01T00:00:00+00:00';cutoff=datetime.now(timezone.utc).isoformat()
        for fields in ({'paid':1},{'marketing_consent':0}):
            await db.upsert_user(101,paid=0,marketing_consent=1,last_activity_at=old,purchase_reminder_eligible_at=old)
            await db.upsert_user(101,**fields);self.assertEqual(await db.get_due_purchase_reminders(cutoff),[])
        await db.revoke_access_for_grant(101,service_today().isoformat());self.assertEqual(await db.get_due_purchase_reminders(cutoff),[])

    async def test_old_expired_buttons_and_deleted_profile(self):
        await self.bind();await self.process();await db.upsert_user(101,purchase_date='2020-01-01')
        for fn,data in ((app.show_plan_week,'plan_week_0'),(app.show_plan_day,'plan_day_0'),(app.show_plan_today,'plan_today'),(app.show_day_recipes,'day_recipes_0'),(app.download_pdf,'download_pdf')):
            cb=callback(data=data);await fn(cb)
            self.assertIn('доступ',str(cb.message.answer.await_args).lower())
        await db.delete_user_data(101);await app.show_plan_day(callback(data='plan_day_0'))

    async def test_menu_recovers_missed_webhook_and_plan(self):
        await self.bind();m=SimpleNamespace(from_user=SimpleNamespace(id=101,username=None),answer=AsyncMock());state=SimpleNamespace(clear=AsyncMock(),update_data=AsyncMock(),set_state=AsyncMock())
        with patch.object(app,'get_yookassa_payment',AsyncMock(return_value=payment())):await app.cmd_menu(m,state)
        self.assertTrue(active_access(await db.get_user(101)));self.assertEqual(len(json.loads((await db.get_user(101))['plan_json'])),30)

    async def test_background_recovers_missed_webhook(self):
        await self.bind()
        with patch.object(app,'get_yookassa_payment',AsyncMock(return_value=payment())):
            task=asyncio.create_task(app.payment_recovery_loop())
            try:
                for _ in range(100):
                    if (await db.get_payment('payment-a') or {}).get('fulfilled_at'):break
                    await asyncio.sleep(.02)
                self.assertTrue(active_access(await db.get_user(101)))
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task

    async def test_fsm_restart_isolation_and_delete(self):
        a,b=sqlite_fsm.SQLiteStorage(),sqlite_fsm.SQLiteStorage();key=StorageKey(bot_id=1,chat_id=101,user_id=101);other=StorageKey(bot_id=1,chat_id=202,user_id=202)
        await a.set_state(key,'survey:age');await a.set_data(key,{'age':30})
        self.assertEqual(await b.get_state(key),'survey:age');self.assertEqual(await b.get_data(key),{'age':30});self.assertEqual(await b.get_data(other),{})
        await db.delete_user_data(101);self.assertIsNone(await b.get_state(key));self.assertEqual(await b.get_data(key),{})

    async def test_fsm_parallel_event_isolation(self):
        key=StorageKey(bot_id=1,chat_id=101,user_id=101);counter=[0];peak=[0]
        async def f():
            async with sqlite_fsm.SQLiteEventIsolation().lock(key):
                counter[0]+=1;peak[0]=max(peak[0],counter[0]);await asyncio.sleep(.025);counter[0]-=1
        await asyncio.gather(*[f() for _ in range(5)]);self.assertEqual(peak[0],1)


class NutritionCase(unittest.TestCase):
    def test_original_formula_unchanged(self):
        self.assertAlmostEqual(n.harris_benedict_bmr('m',80,180,30),1853.632)
        self.assertAlmostEqual(n.harris_benedict_bmr('f',60,165,30),1383.683)
        self.assertEqual(n.calc_target(n.calc_tdee(1800,'sedentary'),'loss'),1728)

    def test_exclusion_combinations_0_1_2_5_8_9(self):
        import itertools
        tags=list(config.ALLERGEN_TAGS);success=errors=0
        for size in (0,1,2,5,8,9):
            for combination in itertools.combinations(tags,size):
                with self.subTest(exclude=combination):
                    try:plan,shopping=n.build_plan(1800,30,set(combination),date(2026,10,10))
                    except ValueError as e:
                        self.assertTrue(any(word in str(e) for word in ('мало','не осталось')));errors+=1;continue
                    success+=1;self.assertEqual(len(plan),30)
                    n.validate_plan_exclusions(plan,set(combination))
                    for day in plan:
                        for meal in day['meals']:
                            self.assertFalse(n.meal_exclusion_tags(meal)&set(combination))
                            for name,amount,unit in meal['ingredients']:
                                if unit=='шт':self.assertEqual(int(amount),amount)
                    self.assertEqual([(g['days_start'],g['days_end']) for g in shopping],[(1,5),(6,10),(11,15),(16,20),(21,25),(26,30)])
        self.assertGreater(success,0);self.assertGreater(errors,0)

    def test_shopping_sums_normalized_from_actual_portions(self):
        from collections import defaultdict
        plan,groups=n.build_plan(1800)
        for i,g in enumerate(groups):
            sums=defaultdict(float)
            for day in plan[i*5:i*5+5]:
                for meal in day['meals']:
                    for name,amount,unit in meal['ingredients']:sums[(n.canonical_product_name(name),unit)]+=amount
            self.assertEqual({(name,unit):amount for name,amount,unit in g['items']},{key:round(value,1) for key,value in sums.items()})
        self.assertEqual(n.canonical_product_name(' Яйцо '),'яйца')

    def test_single_day_recipes_button(self):
        buttons=[b for row in kb.plan_day_kb(0,30).inline_keyboard for b in row]
        self.assertEqual(sum(b.callback_data=='day_recipes_0' for b in buttons),1)
        self.assertEqual(next(b.text for b in buttons if b.callback_data=='day_recipes_0'),'🍽 Рецепты на день')

    def test_access_calendar_boundaries(self):
        purchase=date(2026,10,10);u={'paid':1,'purchase_date':purchase.isoformat()}
        self.assertFalse(active_access(u,purchase-timedelta(days=1)))
        self.assertTrue(active_access(u,purchase));self.assertTrue(active_access(u,purchase+timedelta(days=29)))
        self.assertFalse(active_access(u,purchase+timedelta(days=30)))
        self.assertFalse(active_access({'paid':1,'purchase_date':'bad'},purchase))

    def test_pdf_cyrillic_current_plan(self):
        from pdf_export import generate_pdf
        plan,shopping=n.build_plan(1900,days=6,start_date=date(2026,10,10))
        with tempfile.TemporaryDirectory() as d:
            out=str(Path(d)/'plan.pdf');generate_pdf({'target_kcal':1900,'plan_json':json.dumps(plan,ensure_ascii=False),'shopping_json':json.dumps(shopping,ensure_ascii=False)},out)
            self.assertTrue(Path(out).read_bytes().startswith(b'%PDF-'))
            text=subprocess.check_output(['pdftotext',out,'-']).decode()
            for word in ('Информационный','Меню','покупок','10.10.2026','15.10.2026','1900'):
                self.assertIn(word,text)


class ValidationCase(unittest.TestCase):
    def test_invalid_payment_fields_all_rejected(self):
        cases=[{'test':True},{'test':None},{'recipient':{'account_id':'wrong'}},{'amount':{'value':'88.00','currency':'RUB'}},{'amount':{'value':'NaN','currency':'RUB'}},{'amount':{'value':'89.00','currency':'USD'}},{'metadata':{'user_id':'202'}},{'status':'pending'},{'paid':False},{'id':'different'}]
        for changes in cases:
            with self.subTest(changes=changes),self.assertRaises(ValueError):ps.validate_successful_payment(payment(**changes),101,'payment-a')
        ps.validate_successful_payment(payment(),101,'payment-a')

    def test_production_admin_numeric_only(self):
        self.assertTrue(app.is_admin(SimpleNamespace(id=9001,username=None)))
        self.assertFalse(app.is_admin(SimpleNamespace(id=101,username='gymochka_admin')))


class ProviderHTTPCase(unittest.IsolatedAsyncioTestCase):
    async def test_redirect_create_get_idempotence_contract(self):
        from aiohttp import web
        calls=[]
        async def create(request):
            calls.append((request.headers.get('Idempotence-Key'),await request.json()))
            return web.json_response(payment(status='pending'))
        async def get(request):return web.json_response(payment())
        a=web.Application();a.router.add_post('/payments',create);a.router.add_get('/payments/{id}',get)
        runner=web.AppRunner(a);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        try:
            with patch.object(ps,'YOOKASSA_API_URL',f'http://127.0.0.1:{port}'):
                await ps.create_yookassa_payment(101,'https://local.example/payment/return','same-key')
                await ps.create_yookassa_payment(101,'https://local.example/payment/return','same-key')
                result=await ps.get_yookassa_payment('payment-a');ps.validate_successful_payment(result,101,'payment-a')
            self.assertEqual(calls[0],calls[1]);payload=calls[0][1]
            self.assertEqual(payload['amount'],{'value':'89.00','currency':'RUB'});self.assertIs(payload['capture'],True)
            self.assertEqual(payload['confirmation'],{'type':'redirect','return_url':'https://local.example/payment/return'})
            self.assertEqual(payload['metadata']['user_id'],'101');self.assertEqual(payload['metadata']['order_key'],'same-key')
        finally:await runner.cleanup()


class RuntimeCase(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=DBCase.asyncSetUp
    asyncTearDown=DBCase.asyncTearDown
    bind=DBCase.bind
    process=DBCase.process

    async def test_real_dispatcher_survey_payment_menu_recipes_shopping_pdf(self):
        fake=FakeSession();old=app.bot.session;app.bot.session=fake
        seq=0;uid=303
        async def feed(text=None,data=None):
            nonlocal seq
            seq+=1
            user={'id':uid,'is_bot':False,'first_name':'Synthetic','username':'synthetic_user'}
            msg={'message_id':seq,'date':1700000000,'chat':{'id':uid,'type':'private'},'from':user,'text':text or 'button'}
            update={'update_id':seq,'message':msg} if data is None else {'update_id':seq,'callback_query':{'id':str(seq),'from':user,'chat_instance':'local','data':data,'message':msg}}
            return await app.dp.feed_raw_update(app.bot,update)
        try:
            await feed('/start');self.assertFalse(app.has_consent(await db.get_user(uid)))
            await feed(data='consent_accept');await feed(data='legal_continue')
            await feed(data='demo')
            for text in ('30','180','80'):await feed(text)
            for data in ('gender_m','act_sedentary','goal_maintain','all_eggs','all_done'):await feed(data=data)
            u=await db.get_user(uid);self.assertEqual(len(json.loads(u['plan_json'])),1);self.assertEqual(u['demo_used'],1)
            await feed(data='pay')
            p=payment(user=uid,status='pending',pid='payment-full')
            with patch.object(app,'create_yookassa_payment',AsyncMock(return_value=p)):await feed(data='pay_now')
            with patch.object(app,'get_yookassa_payment',AsyncMock(return_value=payment(user=uid,pid='payment-full'))):await feed(data='check_payment')
            u=await db.get_user(uid);self.assertTrue(active_access(u));self.assertEqual(len(json.loads(u['plan_json'])),30);n.validate_plan_exclusions(json.loads(u['plan_json']),{'eggs'})
            for data in ('show_plan','plan_week_0','plan_day_0','day_recipes_0','show_shopping','download_pdf'):await feed(data=data)
            texts=[getattr(m,'text','') or '' for m in fake.calls]
            self.assertTrue(any('Рецепты' in text for text in texts));self.assertTrue(any('Закупка: дни 1–5' in text for text in texts))
            self.assertTrue(any(m.__api_method__=='sendDocument' for m in fake.calls))
            self.assertIsNone(await app.dp.storage.get_state(StorageKey(bot_id=app.bot.id,chat_id=uid,user_id=uid)))
            # Real opt-out handler and profile deletion; old buttons don't disclose plan.
            await feed(data='marketing_toggle');self.assertEqual((await db.get_user(uid))['marketing_consent'],1)
            await feed('/unsubscribe');self.assertEqual((await db.get_user(uid))['marketing_consent'],0)
            await feed('/privacy');await feed('/delete_data');self.assertIsNone(await db.get_user(uid))
            await feed(data='plan_day_0');self.assertFalse(active_access(await db.get_user(uid)))
        finally:app.bot.session=old

    async def test_survey_resume_after_storage_restart(self):
        key=StorageKey(bot_id=app.bot.id,chat_id=101,user_id=101)
        await app.dp.storage.set_state(key,'Onboarding:waiting_height');await app.dp.storage.set_data(key,{'age':30})
        storage=sqlite_fsm.SQLiteStorage();self.assertEqual(await storage.get_data(key),{'age':30});self.assertEqual(await storage.get_state(key),'Onboarding:waiting_height')

    async def test_passenger_concurrent_requests_loop_runs_in_idle(self):
        spec=importlib.util.spec_from_file_location('passenger_test',ROOT/'passenger_wsgi.py');w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
        started=AsyncMock();updates=AsyncMock();seen=[]
        async def startup():
            await started()
            async def tick():await asyncio.sleep(.05);seen.append('background')
            asyncio.create_task(tick())
        async def process(pid):
            await asyncio.sleep(.025);seen.append(pid)
        w.on_startup=startup;w.on_shutdown=AsyncMock();w.process_yookassa_success=process
        def request(path='/yookassa/webhook',data=None,secret=None):
            data=json.dumps(data or {'type':'notification','event':'payment.succeeded','object':{'id':'payment-a'}}).encode()
            env={'PATH_INFO':path,'REQUEST_METHOD':'POST','CONTENT_LENGTH':str(len(data)),'wsgi.input':io.BytesIO(data)}
            if secret:env['HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN']=secret
            status=[];out=w.application(env,lambda st,headers:status.append(st));return status[0],b''.join(out)
        try:
            results=await asyncio.gather(*[asyncio.to_thread(request) for _ in range(8)])
            self.assertTrue(all(st=='200 OK' for st,body in results));self.assertEqual(started.await_count,1)
            await asyncio.sleep(.1);self.assertIn('background',seen)
            self.assertEqual((await asyncio.to_thread(request,'/webhook'))[0],'403 Forbidden')
            # Return URL has no startup, payment processing or DB writes.
            before=list(seen);self.assertEqual((await asyncio.to_thread(request,'/payment/return'))[0],'200 OK');self.assertEqual(seen,before)
            self.assertTrue(w._LOOP.is_running())
        finally:await asyncio.to_thread(w._close)

    async def test_startup_shutdown_do_not_switch_webhooks(self):
        fake=FakeSession();old=app.bot.session;app.bot.session=fake
        with patch.object(app,'validate_runtime_config'),patch.object(app,'configure_bot_navigation',AsyncMock()):
            try:
                await app.on_startup();await app.on_shutdown()
                self.assertNotIn('setWebhook',[x.__api_method__ for x in fake.calls]);self.assertNotIn('deleteWebhook',[x.__api_method__ for x in fake.calls])
            finally:app.bot.session=old

    async def test_startup_wrong_webhook_rejected_no_change(self):
        with patch.object(app,'validate_runtime_config'),patch.object(app,'configure_bot_navigation',AsyncMock()),patch.object(app.bot,'get_webhook_info',AsyncMock(return_value=SimpleNamespace(url='https://other.example/webhook'))):
            with self.assertRaises(RuntimeError):await app.on_startup()

    async def test_multiple_processes_credit_only_once(self):
        await self.bind(pid="multiprocess-payment")
        code="""import asyncio,sys
sys.path.insert(0,sys.argv[1])
import database as d
async def main():
 await d.complete_yookassa_payment(101,'multiprocess-payment',sys.argv[2],'now','v')
asyncio.run(main())
"""
        env=os.environ.copy();env['DB_PATH']=self.path
        workers=[await asyncio.create_subprocess_exec(sys.executable,'-c',code,str(ROOT/'gymochka_app'),service_today().isoformat(),env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE) for _ in range(4)]
        for worker in workers:
            stdout,stderr=await worker.communicate();self.assertEqual(worker.returncode,0,stderr.decode())
        record=await db.get_payment('multiprocess-payment');self.assertEqual(record['user_id'],101)
        self.assertEqual((await db.get_user(101))['plan_revision'],1)

    async def test_legacy_migration_preserves_sensitive_fields_and_registration(self):
        import sqlite3
        other=str(Path(self.tmp.name)/'legacy.db')
        with sqlite3.connect(other) as c:
            c.execute('CREATE TABLE users (user_id INTEGER PRIMARY KEY, created_at TEXT, weight REAL, paid INTEGER, purchase_date TEXT)')
            c.execute("INSERT INTO users VALUES (909,'2020-01-01',80,1,'2026-10-10')")
        with patch.object(db,'DB_PATH',other):
            await db.init_db();u=await db.get_user(909)
            self.assertEqual(u['created_at'],'2020-01-01');self.assertEqual(u['weight'],80)
            await db.init_db();self.assertEqual((await db.get_user(909))['weight'],80)


class ReleaseCase(unittest.TestCase):
    def test_wal_backup_install_rollback_preserves_env_and_db(self):
        import sqlite3
        spec=importlib.util.spec_from_file_location('release_test',ROOT/'tools/release_manager.py');release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);target=base/'app';target.mkdir();entry=base/'passenger_wsgi.py';entry.write_text('APP_DIR = "old"\n');(target/'bot.py').write_text('original_code = True\n');(target/'.env').write_text('SYNTHETIC_CONFIG=retain\n')
            dbpath=target/'clients.sqlite3';conn=sqlite3.connect(dbpath);conn.execute('PRAGMA journal_mode=WAL');conn.execute('CREATE TABLE marker (value TEXT)');conn.execute("INSERT INTO marker VALUES ('synthetic')");conn.commit()
            try:
                release.backup(target,dbpath,entry,base/'backup')
                with sqlite3.connect(base/'backup/database.sqlite3') as snap:self.assertEqual(snap.execute('SELECT value FROM marker').fetchone()[0],'synthetic')
                before_env=(target/'.env').read_bytes();before_db=dbpath.read_bytes()
                release.install(target,entry,base/'backup',True)
                self.assertEqual((target/'.env').read_bytes(),before_env);self.assertEqual(dbpath.read_bytes(),before_db)
                self.assertIn(repr(str(target.resolve())),entry.read_text())
                # Post-backup client activity must survive code rollback.
                conn.execute("INSERT INTO marker VALUES ('new synthetic')");conn.commit()
                release.rollback(base/'backup',True)
                self.assertEqual((target/'bot.py').read_text(),'original_code = True\n');self.assertEqual((target/'.env').read_bytes(),before_env)
                self.assertEqual(conn.execute('SELECT count(*) FROM marker').fetchone()[0],2)
            finally:conn.close()


if __name__=='__main__':unittest.main(verbosity=2)

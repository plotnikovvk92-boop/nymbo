"""Passenger WSGI bridge. No polling and no webhook changes on startup.

APP_DIR is intentionally the original path. A separate test entrypoint must
point at its confirmed directory; never install this file over both roots.
"""
import asyncio
import atexit
import concurrent.futures
import hmac
import json
import os
import sys
import threading

APP_DIR = "/home/p/plotnixu/plotnixu.beget.tech/gymochka_app"
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from bot import bot, dp, on_startup, on_shutdown, process_yookassa_success, get_webhook_status
from config import WEBHOOK_PATH, WEBHOOK_SECRET_TOKEN, YOOKASSA_WEBHOOK_PATH

_LOOP = None
_STARTED = False
_PID = None
_START_LOCK = threading.Lock()
_THREAD = None
MAX_BODY = 1024 * 1024


def _ensure_started():
    global _LOOP, _THREAD, _STARTED, _PID
    with _START_LOCK:
        if _STARTED and _PID == os.getpid(): return
        if _LOOP is None or _PID != os.getpid():
            _PID = os.getpid()
            _LOOP = asyncio.new_event_loop()
            ready = threading.Event()
            def run():
                asyncio.set_event_loop(_LOOP)
                ready.set()
                _LOOP.run_forever()
            _THREAD = threading.Thread(target=run, name='nyambo-async', daemon=True)
            _THREAD.start()
            ready.wait()
        asyncio.run_coroutine_threadsafe(on_startup(), _LOOP).result(timeout=60)
        _STARTED = True


def _submit(coroutine):
    # Requests may enter on different WSGI threads; only this loop touches Bot/DP.
    # Do not cancel a payment transaction because a client disconnected.
    return asyncio.run_coroutine_threadsafe(coroutine, _LOOP).result(timeout=120)


def _close():
    if _STARTED and _PID == os.getpid() and _LOOP and _LOOP.is_running():
        try: asyncio.run_coroutine_threadsafe(on_shutdown(), _LOOP).result(timeout=10)
        except Exception: pass
        _LOOP.call_soon_threadsafe(_LOOP.stop)
atexit.register(_close)


def _json_response(start_response, status, payload):
    data=(json.dumps(payload, ensure_ascii=False)+'\n').encode('utf-8')
    start_response(status,[('Content-Type','application/json; charset=utf-8'),('Content-Length',str(len(data)))])
    return [data]


def _body(environ):
    size=int(environ.get('CONTENT_LENGTH') or 0)
    if not 0 < size <= MAX_BODY: raise ValueError('invalid body size')
    data=json.loads(environ['wsgi.input'].read(size).decode('utf-8'))
    if not isinstance(data,dict): raise ValueError('object required')
    return data


def application(environ, start_response):
    path=environ.get('PATH_INFO','/')
    method=environ.get('REQUEST_METHOD','GET')
    try:
        if path in (WEBHOOK_PATH, YOOKASSA_WEBHOOK_PATH):
            if method != 'POST': return _json_response(start_response,'405 Method Not Allowed',{'ok':False})
            if path == WEBHOOK_PATH:
                secret=environ.get('HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN','')
                if not WEBHOOK_SECRET_TOKEN or not hmac.compare_digest(secret,WEBHOOK_SECRET_TOKEN):
                    return _json_response(start_response,'403 Forbidden',{'ok':False})
            try: payload=_body(environ)
            except (ValueError,KeyError,UnicodeError): return _json_response(start_response,'400 Bad Request',{'ok':False})
            _ensure_started()
            if get_webhook_status() == 'mismatch':
                return _json_response(start_response,'503 Service Unavailable',{'ok':False,'error':'webhook_mismatch'})
            if path == WEBHOOK_PATH:
                _submit(dp.feed_raw_update(bot,payload))
            elif payload.get('type') == 'notification' and payload.get('event') == 'payment.succeeded':
                payment_id=(payload.get('object') or {}).get('id')
                if not isinstance(payment_id,str): return _json_response(start_response,'400 Bad Request',{'ok':False})
                try: _submit(process_yookassa_success(payment_id))
                except ValueError: return _json_response(start_response,'400 Bad Request',{'ok':False,'error':'payment_rejected'})
            return _json_response(start_response,'200 OK',{'ok':True})
        if path == '/health':
            _ensure_started()
            state = get_webhook_status()
            if state == 'mismatch':
                return _json_response(start_response,'503 Service Unavailable',{'ok':False,'error':'webhook_mismatch'})
            return _json_response(start_response,'200 OK',{'ok':True,'service':'nyambo','telegram_webhook':state})
        if path == '/payment/return':
            # Deliberately no initialization, API call or DB write on a redirect.
            data='Вернитесь в Telegram и нажмите «Проверить оплату» или /start. Доступ выдаётся только после проверки платежа через API ЮKassa.\n'.encode('utf-8')
            start_response('200 OK',[('Content-Type','text/plain; charset=utf-8'),('Content-Length',str(len(data)))])
            return [data]
        return _json_response(start_response,'404 Not Found',{'ok':False})
    except Exception as error:
        print('NYAMBO_REQUEST_ERROR type='+type(error).__name__,file=sys.stderr)
        return _json_response(start_response,'500 Internal Server Error',{'ok':False,'error':'temporary_error'})

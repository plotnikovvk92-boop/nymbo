"""Route two applications through one HTTPS site, in isolated interpreters.

Install alongside passenger_app_bridge.py; neither child runs polling.
"""
import base64
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path('/home/p/plotnixu/plotnixu.beget.tech')
HERE = Path(__file__).resolve().parent
MAX_BODY = 1024 * 1024
ROUTES = {
    '/webhook': ('test', '/webhook'),
    '/yookassa/webhook': ('test', '/yookassa/webhook'),
    '/health': ('test', '/health'),
    '/production/webhook': ('production', '/production/webhook'),
    '/production/yookassa/webhook': ('production', '/production/yookassa/webhook'),
    '/production/health': ('production', '/health'),
}
APPS = {'test': 'gymochka_app', 'production': 'nyambo_app'}


def response(start_response, status, payload):
    body = (json.dumps(payload) + '\n').encode()
    start_response(status, [('Content-Type', 'application/json'),
                            ('Content-Length', str(len(body)))])
    return [body]


class Worker:
    def __init__(self, role):
        self.role = role
        self.pid = os.getpid()
        self.lock = threading.Lock()
        self.replies = queue.Queue()
        self.process = subprocess.Popen(
            [sys.executable, str(HERE / 'passenger_gateway.py'), '--worker', role],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            bufsize=1, close_fds=True,
        )
        def receive():
            try:
                for line in self.process.stdout:
                    self.replies.put(json.loads(line))
            except Exception:
                pass
            finally:
                self.replies.put(None)
        threading.Thread(target=receive, daemon=True).start()

    def request(self, request):
        with self.lock:
            request['id'] = uuid.uuid4().hex
            self.process.stdin.write(json.dumps(request) + '\n')
            self.process.stdin.flush()
            deadline = time.monotonic() + 130
            while True:
                reply = self.replies.get(timeout=max(0, deadline - time.monotonic()))
                if reply is None:
                    raise RuntimeError('worker stopped')
                if reply.get('id') == request['id']:
                    return reply
                # A timed-out request may finish later; never reuse its reply.


_workers = {}
_lock = threading.Lock()


def worker(role):
    with _lock:
        current = _workers.get(role)
        if current is None or current.pid != os.getpid() or current.process.poll() is not None:
            current = _workers[role] = Worker(role)
        return current


def application(environ, start_response):
    path = environ.get('PATH_INFO', '/')
    if path == '/payment/return':
        body = 'Вернитесь в Telegram и нажмите «Проверить оплату» или /start.\n'.encode()
        start_response('200 OK', [('Content-Type', 'text/plain; charset=utf-8'),
                                  ('Content-Length', str(len(body)))])
        return [body]
    route = ROUTES.get(path)
    if route is None:
        return response(start_response, '404 Not Found', {'ok': False})
    try:
        size = int(environ.get('CONTENT_LENGTH') or 0)
        if size < 0 or size > MAX_BODY:
            return response(start_response, '413 Payload Too Large', {'ok': False})
        body = environ['wsgi.input'].read(size) if size else b''
        role, forwarded_path = route
        request = {
            'path': forwarded_path,
            'method': environ.get('REQUEST_METHOD', 'GET'),
            'secret': environ.get('HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN', ''),
            'body': base64.b64encode(body).decode(),
        }
        reply = worker(role).request(request)
        decoded = base64.b64decode(reply['body'])
        status, headers = reply['status'], reply['headers']
    except Exception as error:
        print('NYAMBO_GATEWAY_ERROR type=' + type(error).__name__, file=sys.stderr)
        return response(start_response, '503 Service Unavailable', {'ok': False})
    start_response(status, headers)
    return [decoded]


def serve(role):
    # stdout is exclusively the private IPC protocol. App messages go to stderr.
    output = sys.stdout
    sys.stdout = sys.stderr
    app = ROOT / APPS[role]
    sys.path.insert(0, str(app))
    from dotenv import dotenv_values
    import ast
    config_tree = ast.parse((app / 'config.py').read_text())
    config_names = {node.args[0].value for node in ast.walk(config_tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'getenv' and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)}
    # Remove inherited app variables; each child reads only its own .env.
    for name in config_names:
        os.environ.pop(name, None)
    values = dotenv_values(app / '.env')
    for name, value in values.items():
        if value is not None:
            os.environ[name] = value
    expected = '/webhook' if role == 'test' else '/production/webhook'
    expected_payment = '/yookassa/webhook' if role == 'test' else '/production/yookassa/webhook'
    if values.get('WEBHOOK_PATH', '/webhook') != expected:
        raise RuntimeError('configured webhook path differs from role')
    if values.get('YOOKASSA_WEBHOOK_PATH', '/yookassa/webhook') != expected_payment:
        raise RuntimeError('configured payment path differs from role')
    if role == 'production' and values.get('TEST_MODE', '').lower() != 'false':
        raise RuntimeError('production test mode must be disabled')
    scope = {'__name__': 'nyambo_app_bridge', '__file__': str(HERE / 'passenger_app_bridge.py')}
    source = (HERE / 'passenger_app_bridge.py').read_text()
    source = source.replace(str(ROOT / 'gymochka_app'), str(app))
    exec(compile(source, scope['__file__'], 'exec'), scope)
    try:
        for line in sys.stdin:
            request = json.loads(line)
            body = base64.b64decode(request['body'])
            captured = []
            environ = {'PATH_INFO': request['path'], 'REQUEST_METHOD': request['method'],
                       'CONTENT_LENGTH': str(len(body)), 'wsgi.input': io.BytesIO(body),
                       'HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN': request['secret']}
            result = scope['application'](environ, lambda status, headers: captured.append((status, headers)))
            try:
                data = b''.join(result)
            finally:
                if hasattr(result, 'close'):
                    result.close()
            status, headers = captured[0]
            output.write(json.dumps({'id': request['id'], 'status': status,
                                     'headers': headers, 'body': base64.b64encode(data).decode()}) + '\n')
            output.flush()
    finally:
        scope['_close']()


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--worker' or sys.argv[2] not in APPS:
        raise SystemExit('worker arguments required')
    serve(sys.argv[2])

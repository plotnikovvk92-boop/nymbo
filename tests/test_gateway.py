import base64
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

GATEWAY = Path(__file__).resolve().parents[1] / 'deployment/passenger_gateway.py'


class GatewayTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('gateway_test', GATEWAY)
        self.gateway = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gateway)

    def request(self, path, body=b'', secret=''):
        captured = []
        result = self.gateway.application(
            {'PATH_INFO': path, 'REQUEST_METHOD': 'POST',
             'CONTENT_LENGTH': str(len(body)), 'wsgi.input': io.BytesIO(body),
             'HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN': secret},
            lambda status, headers: captured.append(status))
        return captured[0], b''.join(result)

    def test_routes_keep_test_and_production_separate(self):
        seen = []
        class FakeWorker:
            def request(_, request):
                seen.append(request)
                return {'status': '200 OK', 'headers': [], 'body': base64.b64encode(b'ok').decode()}
        with patch.object(self.gateway, 'worker', return_value=FakeWorker()) as choose:
            self.request('/webhook', b'{"update_id":1}', 'test-secret')
            choose.assert_called_with('test')
            self.request('/production/webhook', b'{"update_id":2}', 'production-secret')
            choose.assert_called_with('production')
        self.assertEqual(seen[0]['path'], '/webhook')
        self.assertEqual(seen[1]['path'], '/production/webhook')
        self.assertNotEqual(seen[0]['secret'], seen[1]['secret'])
        self.assertEqual(base64.b64decode(seen[1]['body']), b'{"update_id":2}')

    def test_return_and_unknown_paths_never_start_apps(self):
        with patch.object(self.gateway, 'worker') as worker:
            self.assertEqual(self.request('/payment/return')[0], '200 OK')
            self.assertEqual(self.request('/unknown')[0], '404 Not Found')
            worker.assert_not_called()

    def test_child_failure_does_not_route_to_other_bot(self):
        with patch.object(self.gateway, 'worker', side_effect=RuntimeError('offline')):
            self.assertEqual(self.request('/production/health')[0], '503 Service Unavailable')

    def test_real_children_use_separate_envs_and_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = GATEWAY.read_text().replace(
                "Path('/home/p/plotnixu/plotnixu.beget.tech')", repr(root))
            # repr(Path) requires PosixPath; replace with portable constructor.
            source = source.replace(repr(root), 'Path(' + repr(str(root)) + ')')
            (root / 'passenger_gateway.py').write_text(source)
            (root / 'passenger_app_bridge.py').write_text(
                'import config\n'
                'def application(env, start):\n'
                ' start("200 OK", [])\n'
                ' return [config.VALUE.encode()]\n'
                'def _close(): pass\n')
            for name, value, path in [('gymochka_app', 'TEST', '/webhook'),
                                      ('nyambo_app', 'PRODUCTION', '/production/webhook')]:
                app = root / name
                app.mkdir()
                (app / 'config.py').write_text('import os\nVALUE = os.getenv("VALUE")\n')
                (app / '.env').write_text('VALUE=' + value + '\nTEST_MODE=False\nWEBHOOK_PATH=' + path
                    + '\nYOOKASSA_WEBHOOK_PATH=' + ('/yookassa/webhook' if value == 'TEST' else '/production/yookassa/webhook') + '\n')
            with patch.object(self.gateway, 'HERE', root):
                workers = [self.gateway.Worker('test'), self.gateway.Worker('production')]
                try:
                    results = [w.request({'path': '/health', 'method': 'GET', 'secret': '', 'body': ''}) for w in workers]
                    self.assertEqual([base64.b64decode(r['body']) for r in results], [b'TEST', b'PRODUCTION'])
                    self.assertNotEqual(workers[0].process.pid, workers[1].process.pid)
                finally:
                    for w in workers:
                        w.process.stdin.close()
                        w.process.wait(timeout=10)
                        w.process.stdout.close()


if __name__ == '__main__':
    unittest.main()

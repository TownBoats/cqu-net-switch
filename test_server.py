import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from server import AppError, Dashboard, Profiles, make_server, normalize_status, parse_jsonp, protect


def status(account=''):
    return {'online': bool(account), 'account': account}


class FakePortal:
    def __init__(self, states):
        self.states = iter(states)
        self.calls = []

    def status(self):
        self.calls.append('status')
        state = next(self.states)
        if isinstance(state, Exception):
            raise state
        return state

    def logout(self):
        self.calls.append('logout')

    def prepare(self):
        pass

    def login(self, account, password):
        self.calls.append(('login', account))


class PortalTests(unittest.TestCase):
    def test_gbk_jsonp_and_portal_units(self):
        source = ' cb({"result":1,"uid":"testT","NID":"测试","flow":1048576,"time":"125 ","olmac":"aabbccddeeff"}); '
        data = normalize_status(parse_jsonp(source.encode('gbk').decode('gbk')))
        self.assertEqual(data['bytes'], 1024 ** 3)
        self.assertEqual(data['minutes'], 125)
        self.assertEqual(data['mac'], 'AA:BB:CC:DD:EE:FF')
        self.assertEqual(data['name'], '测试')

    def test_offline_and_malformed_are_distinct(self):
        self.assertFalse(normalize_status(parse_jsonp('cb({"result":0})'))['online'])
        for invalid in ('<html>failed</html>', 'cb({})', 'cb({"result":2})', 'cb({"result":0});alert(1)'):
            with self.assertRaises(AppError):
                parse_jsonp(invalid)

    def run_switch(self, states, action='switch'):
        portal = FakePortal(states)
        app = Dashboard(None, portal, pause=lambda _: None)
        app.run(action, {'account': 'targetT'}, 'secret')
        return app, portal

    def test_switch_verifies_offline_before_login_and_target_after(self):
        app, portal = self.run_switch([status('oldT'), status(), status(), status('targetT')])
        self.assertEqual(portal.calls, ['status', 'logout', 'status', ('login', 'targetT'), 'status', 'status'])
        self.assertEqual(app.operation['state'], 'success')

    def test_failed_logout_does_not_attempt_login(self):
        app, portal = self.run_switch([status('oldT')] * 6)
        self.assertEqual(app.operation['state'], 'error')
        self.assertFalse(any(isinstance(call, tuple) for call in portal.calls))

    def test_wrong_account_is_not_success(self):
        app, _ = self.run_switch([status()] + [status('wrongT')] * 5)
        self.assertEqual(app.operation['state'], 'error')

    def test_unknown_status_does_not_logout(self):
        app, portal = self.run_switch([AppError('unavailable')])
        self.assertEqual(app.operation['state'], 'error')
        self.assertEqual(portal.calls, ['status'])

    def test_already_online_is_noop(self):
        app, portal = self.run_switch([status('targetT')])
        self.assertEqual(app.operation['state'], 'success')
        self.assertEqual(portal.calls, ['status'])

    def test_offline_logout_is_noop(self):
        app, portal = self.run_switch([status()], 'logout')
        self.assertEqual(app.operation['state'], 'success')
        self.assertEqual(portal.calls, ['status'])

    def test_transient_status_failure_retries(self):
        app, _ = self.run_switch([status(), AppError('retry'), status('targetT')])
        self.assertEqual(app.operation['state'], 'success')

    def test_busy_rejects_parallel_operation(self):
        app = Dashboard(None, FakePortal([]))
        app.operation = {'state': 'running'}
        with self.assertRaises(AppError):
            app.start('logout')

    def test_unreadable_password_does_not_disconnect(self):
        portal = FakePortal([])
        with tempfile.TemporaryDirectory() as directory:
            profiles = Profiles(Path(directory) / 'profiles.json')
            profiles.items = [{'id': '1', 'account': 'targetT', 'label': 'test', 'secret': 'invalid'}]
            app = Dashboard(profiles, portal)
            with patch('core.protect', side_effect=AppError('failed')):
                with self.assertRaises(AppError):
                    app.start('switch', '1')
            self.assertEqual(portal.calls, [])


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
class ProfileTests(unittest.TestCase):
    def test_encrypted_roundtrip_edit_restart_duplicate_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.json'
            profiles = Profiles(path)
            profiles.save({'account': 'testT', 'label': '测试', 'password': 'test-P@ss中文'})
            self.assertNotIn('test-P@ss', path.read_text(encoding='utf-8'))
            self.assertNotIn('secret', json.dumps(profiles.public()))
            profile_id = profiles.public()[0]['id']
            self.assertEqual(protect(profiles.get(profile_id)['secret'], True), 'test-P@ss中文')
            profiles.save({'id': profile_id, 'account': 'testT', 'label': '新备注', 'password': ''})
            reopened = Profiles(path)
            self.assertEqual(protect(reopened.get(profile_id)['secret'], True), 'test-P@ss中文')
            with self.assertRaises(AppError):
                reopened.save({'account': 'testT', 'password': 'duplicate'})
            with self.assertRaises(AppError):
                reopened.save({'id': profile_id, 'account': 'anotherT', 'password': ''})
            reopened.delete(profile_id)
            self.assertEqual(Profiles(path).public(), [])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.server = make_server(0, self.directory.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, path, data=None, headers=None):
        return urlopen(Request(self.origin + path, data=data, headers=headers or {}), timeout=3)

    def test_assets_bootstrap_and_csrf_protection(self):
        with self.request('/') as response:
            self.assertIn('frame-ancestors', response.headers['Content-Security-Policy'])
            self.assertIn('山城连接', response.read().decode('utf-8'))
        with self.request('/api/bootstrap') as response:
            bootstrap = json.load(response)
            self.assertTrue(bootstrap['token'])
        for headers in ({}, {'Origin': self.origin}, {'Origin': 'https://evil.example', 'X-App-Token': bootstrap['token']}):
            with self.assertRaises(HTTPError) as caught:
                self.request('/api/logout', b'{}', headers)
            self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/bootstrap', headers={'Host': 'evil.example'})
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(self.server.app.operation['state'], 'idle')

    def test_status_errors_are_not_reported_offline(self):
        self.server.app.portal = FakePortal([AppError('unavailable')])
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/status', headers={'X-App-Token': self.server.token})
        self.assertEqual(caught.exception.code, 502)
        self.assertIn('error', json.load(caught.exception))


if __name__ == '__main__':
    unittest.main()

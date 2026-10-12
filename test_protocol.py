import base64
import json
import unittest
from unittest.mock import patch

from core import (AppError, Dashboard, Portal, export_profiles_text,
                  normalize_status, parse_profiles_import)


class ProtocolTests(unittest.TestCase):
    def portal(self):
        portal = Portal()
        portal.context = dict(ipv4='10.0.0.8', ipv6='', mac='AABBCCDDEEFF', account='oldT', vlan='1')
        portal.config = dict(login_method='1', account_prefix='1', un_bind_mac='1', register_mode='1', ac_logout='1')
        return portal

    def test_current_school_login_uses_portal_protocol_and_prefix(self):
        portal = self.portal()
        with patch.object(portal, 'jsonp', return_value={'result': 1}) as request:
            portal.login('targetT', 'dummy-password')
        path, params = request.call_args.args
        self.assertEqual(path, ':802/eportal/portal/login')
        self.assertEqual(params['user_account'], ',0,targetT')
        self.assertEqual(params['user_password'], 'dummy-password')
        self.assertEqual(params['wlan_user_mac'], 'AABBCCDDEEFF')
        self.assertEqual(params['wlan_user_ip'], '10.0.0.8')

    def test_logout_unbinds_only_current_device(self):
        portal = self.portal()
        with patch.object(portal, 'jsonp', return_value={'result': 1}) as request:
            portal.logout()
        path, params = request.call_args.args
        self.assertEqual(path, ':802/eportal/portal/mac/unbind')
        self.assertEqual(params['user_account'], 'oldT')
        self.assertEqual(params['wlan_user_mac'], 'AABBCCDDEEFF')
        self.assertEqual(params['wlan_user_ip'], '10.0.0.8')

    def test_missing_mac_does_not_unbind_anyone(self):
        portal = self.portal()
        portal.context['mac'] = ''
        with patch.object(portal, 'jsonp') as request:
            with self.assertRaises(AppError):
                portal.logout()
        request.assert_not_called()

    def test_error_response_is_visible_and_redacts_password(self):
        portal = self.portal()
        with patch.object(portal, 'jsonp', return_value={'result': 0, 'msg': '密码错误: dummy-secret'}):
            with self.assertRaises(AppError) as caught:
                portal.login('targetT', 'dummy-secret')
        self.assertIn('密码错误', str(caught.exception))
        self.assertNotIn('dummy-secret', str(caught.exception))

    def test_config_uses_encoded_client_ip(self):
        portal = self.portal()
        with patch.object(portal, 'jsonp', return_value={'code': 1, 'data': portal.config}) as request:
            portal.prepare()
        self.assertEqual(request.call_args.args[1]['wlan_user_ip'], base64.b64encode(b'10.0.0.8').decode())

    def test_unsupported_configuration_stops_before_logout(self):
        portal = self.portal()
        with patch.object(portal, 'status', return_value={'online': True, 'account': 'oldT'}), \
             patch.object(portal, 'jsonp', return_value={'code': 1, 'data': {'login_method': '99'}}), \
             patch.object(portal, 'logout') as logout:
            app = Dashboard(None, portal)
            app.run('switch', {'account': 'targetT'}, 'dummy-password')
        self.assertEqual(app.operation['state'], 'error')
        logout.assert_not_called()

    def test_offline_ip_is_retained_without_online_fields(self):
        status = normalize_status({'result': 0, 'ss5': '10.0.0.8', 'ss4': '000000000000'})
        self.assertEqual(status['ipv4'], '10.0.0.8')
        self.assertFalse(status['online'])

    def test_online_devices_normalizes_terminal_list(self):
        portal = self.portal()
        payload = {'result': 1, 'list': [
            {'online_session': 1, 'online_time': '2026-10-11 18:05:20', 'online_ip': '10.0.0.8',
             'online_mac': 'aabbccddeeff', 'time_long': '348', 'user_account': 'oldT',
             'is_bind_mac': '1', 'is_perceive': '0', 'is_owner_ip': '1', 'device_alias': ''},
            {'online_ip': '10.0.0.9', 'online_mac': 'invalid', 'is_bind_mac': '0'}]}
        with patch.object(portal, 'jsonp', return_value=payload) as request:
            devices = portal.online_devices()
        path, params = request.call_args.args
        self.assertEqual(path, ':802/eportal/portal/online_list')
        self.assertEqual(params['user_account'], '')
        self.assertEqual(params['wlan_user_mac'], 'AABBCCDDEEFF')
        self.assertEqual(params['wlan_user_ip'], '10.0.0.8')
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0]['mac'], 'AA:BB:CC:DD:EE:FF')
        self.assertEqual(devices[0]['seconds'], 348)
        self.assertTrue(devices[0]['owner'])
        self.assertTrue(devices[0]['bound'])
        self.assertFalse(devices[0]['perceive'])
        self.assertEqual(devices[1]['mac'], 'invalid')

    def test_online_devices_failure_is_app_error(self):
        portal = self.portal()
        with patch.object(portal, 'jsonp', return_value={'result': 0, 'msg': '无在线信息'}):
            with self.assertRaises(AppError) as caught:
                portal.online_devices()
        self.assertIn('无在线信息', str(caught.exception))

    def test_export_import_roundtrip_and_wrong_password(self):
        items = [dict(account='aT', label='宿舍', password='secret-1'),
                 dict(account='bT', label='', password='secret-2')]
        text = export_profiles_text(items, 'pw-123')
        self.assertNotIn('secret-1', text)  # '-' is outside the base64 alphabet.
        self.assertNotIn('secret-2', text)
        self.assertEqual(parse_profiles_import(text, 'pw-123'), items)
        with self.assertRaises(AppError) as caught:
            parse_profiles_import(text, 'pw-124')
        self.assertIn('密码不正确', str(caught.exception))

    def test_import_rejects_tampered_or_foreign_files(self):
        text = export_profiles_text([dict(account='aT', label='', password='secret-1')], 'pw')
        doc = json.loads(text)
        doc['payload'] = doc['payload'][:-4] + 'AAAA'
        with self.assertRaises(AppError):
            parse_profiles_import(json.dumps(doc), 'pw')
        with self.assertRaises(AppError):
            parse_profiles_import('{"hello": 1}', 'pw')
        with self.assertRaises(AppError):
            parse_profiles_import('not json at all', 'pw')

    def test_changing_network_does_not_reuse_old_mac(self):
        portal = self.portal()
        with patch.object(portal, 'fetch', return_value='cb({"result":0,"ss5":"10.1.1.1","ss4":"000000000000"})'):
            portal.status()
        self.assertEqual(portal.context['mac'], '')


if __name__ == '__main__':
    unittest.main()

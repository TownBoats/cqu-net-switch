import gc
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from core import AppError, Dashboard, Profiles, now
from tray import (MENU_ID_ACCOUNT, MENU_ID_AUTOSTART, MENU_ID_LOGOUT, MENU_ID_PANEL,
                  MENU_ID_QUIT, MENU_ID_REFRESH, TIP_LIMIT, TrayApp, build_menu,
                  icon_state, tooltip_text)


class TrayPortal:
    def __init__(self, fail_login=False):
        self.account = 'oldT'
        self.calls = []
        self.fail_login = fail_login

    def status(self):
        return dict(online=bool(self.account), account=self.account, name='测试', ipv4='10.0.0.8',
                    ipv6='', mac='AA:BB:CC:DD:EE:FF', minutes=60, bytes=1024, checkedAt=now())

    def prepare(self):
        self.calls.append('prepare')

    def logout(self):
        self.calls.append('logout')
        self.account = ''

    def login(self, account, password):
        self.calls.append('login')
        if self.fail_login:
            raise AppError('登录失败：模拟密码错误')
        self.account = account


def status_dict(account='targetT', online=True):
    return dict(online=online, account=account if online else '', name='测试' if online else '',
                ipv4='10.0.0.8', ipv6='', mac='', minutes=1, bytes=0, checkedAt=now())


class MenuModelTests(unittest.TestCase):
    def setUp(self):
        self.profiles = [dict(id='a', account='firstT', label='宿舍'),
                         dict(id='b', account='secondT', label='secondT')]

    def test_online_menu_checks_current_account(self):
        entries = build_menu(status_dict('firstT'), False, False, '', self.profiles)
        items = {e.get('id'): e for e in entries if e.get('id', 0) >= MENU_ID_ACCOUNT}
        self.assertTrue(items[MENU_ID_ACCOUNT]['checked'])
        self.assertFalse(items[MENU_ID_ACCOUNT + 1]['checked'])
        self.assertIn('宿舍（firstT）', items[MENU_ID_ACCOUNT]['text'])
        self.assertEqual(items[MENU_ID_ACCOUNT + 1]['text'], 'secondT')  # label == account is not repeated
        logout = next(e for e in entries if e.get('id') == MENU_ID_LOGOUT)
        self.assertTrue(logout['enabled'])

    def test_offline_menu_disables_logout(self):
        entries = build_menu(status_dict(online=False), False, False, '', self.profiles)
        self.assertIn('未登录', entries[0]['text'])
        logout = next(e for e in entries if e.get('id') == MENU_ID_LOGOUT)
        self.assertFalse(logout['enabled'])
        items = [e for e in entries if e.get('id', 0) >= MENU_ID_ACCOUNT]
        self.assertTrue(all(item['enabled'] and not item['checked'] for item in items))

    def test_busy_menu_locks_accounts_and_quit(self):
        entries = build_menu(status_dict(), False, True, '正在登录目标账号…', self.profiles)
        self.assertIn('正在登录目标账号…', entries[0]['text'])
        locked = [e for e in entries if e.get('id') in (MENU_ID_ACCOUNT, MENU_ID_ACCOUNT + 1,
                                                        MENU_ID_REFRESH, MENU_ID_LOGOUT, MENU_ID_QUIT)]
        self.assertTrue(all(not e['enabled'] for e in locked))
        panel = next(e for e in entries if e.get('id') == MENU_ID_PANEL)
        self.assertTrue(panel['enabled'])

    def test_empty_profiles_show_placeholder(self):
        entries = build_menu(None, True, False, '', [])
        self.assertIn('尚未保存账号', entries[1]['text'])
        self.assertFalse(entries[1]['enabled'])

    def test_stale_status_not_treated_as_offline(self):
        entries = build_menu(status_dict(), True, False, '', self.profiles)
        self.assertIn('状态未知', entries[0]['text'])
        items = [e for e in entries if e.get('id', 0) >= MENU_ID_ACCOUNT]
        self.assertTrue(all(not item['checked'] for item in items))

    def test_autostart_entry_reflects_state(self):
        entries = build_menu(status_dict(), False, False, '', self.profiles, autostart=True)
        entry = next(e for e in entries if e.get('id') == MENU_ID_AUTOSTART)
        self.assertTrue(entry['checked'])
        entries = build_menu(status_dict(), False, False, '', self.profiles, autostart=False)
        entry = next(e for e in entries if e.get('id') == MENU_ID_AUTOSTART)
        self.assertFalse(entry['checked'])
        entries = build_menu(status_dict(), False, True, '正在登录…', self.profiles, autostart=True)
        entry = next(e for e in entries if e.get('id') == MENU_ID_AUTOSTART)
        self.assertFalse(entry['enabled'])  # Locked while an operation is running.


@unittest.skipUnless(os.name == 'nt', 'Windows registry')
class AutostartTests(unittest.TestCase):
    def test_registry_roundtrip_uses_launch_command(self):
        import tray
        original = tray.RUN_VALUE
        tray.RUN_VALUE = 'CQU-Net-Switch-Test'
        try:
            tray.set_autostart(False)  # Clean slate, in case a previous run was interrupted.
            self.assertFalse(tray.autostart_enabled())
            tray.set_autostart(True)
            self.assertTrue(tray.autostart_enabled())
            tray.set_autostart(False)
            self.assertFalse(tray.autostart_enabled())
            tray.set_autostart(False)  # Deleting twice must not raise.
        finally:
            tray.RUN_VALUE = original


class TooltipTests(unittest.TestCase):
    def test_states(self):
        self.assertEqual(icon_state(status_dict(), False, False), 'online')
        self.assertEqual(icon_state(status_dict(online=False), False, False), 'offline')
        self.assertEqual(icon_state(status_dict(), True, False), 'offline')
        self.assertEqual(icon_state(status_dict(), False, True), 'busy')

    def test_text_and_limit(self):
        self.assertIn('targetT', tooltip_text(status_dict(), False, False, ''))
        self.assertIn('未登录', tooltip_text(status_dict(online=False), False, False, ''))
        self.assertIn('状态未知', tooltip_text(None, True, False, ''))
        long_name = status_dict()
        long_name['name'] = '很长的姓名' * 40
        self.assertLessEqual(len(tooltip_text(long_name, False, False, '')), TIP_LIMIT)
        self.assertIn('正在登录', tooltip_text(status_dict(), False, True, '正在登录目标账号…'))


@unittest.skipUnless(os.name == 'nt', 'Windows tray')
class TrayAppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.profiles = Profiles(Path(self.directory.name) / 'profiles.json')
        self.profiles.save({'account': 'targetT', 'password': 'dummy-test-password'})
        self.root = tk.Tk()
        self.root.withdraw()
        self.portal = TrayPortal()
        self.app = TrayApp(self.root, self.profiles, Dashboard(self.profiles, self.portal))
        self.app.service.pause = lambda _: None
        self.app.start()

    def tearDown(self):
        self.app.quit(force=True)
        # Finalize Tk wrapper objects on the main thread; collecting them later
        # inside a worker thread aborts the process (Tcl_AsyncDelete).
        gc.collect()
        self.directory.cleanup()

    def complete(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.root.update()
            operation = self.app.service.snapshot()['operation']
            if operation['state'] in ('success', 'error') and not self.app.querying:
                return operation
            time.sleep(.02)
        self.fail('Tray operation did not finish')

    def drain(self, seconds=.6):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(.02)

    def test_account_command_switches_and_checks_menu(self):
        self.app.handle_command(MENU_ID_ACCOUNT)  # First (only) saved account.
        operation = self.complete()
        self.assertEqual(operation['state'], 'success')
        self.assertEqual(self.portal.calls, ['prepare', 'logout', 'login'])
        self.drain()
        self.assertFalse(self.app.stale)
        self.assertEqual(self.app.status['account'], 'targetT')
        entries = build_menu(self.app.status, self.app.stale, self.app.busy(), '', self.profiles.public())
        item = next(e for e in entries if e.get('id') == MENU_ID_ACCOUNT)
        self.assertTrue(item['checked'])

    def test_failed_switch_surfaces_error(self):
        self.portal.fail_login = True
        self.app.handle_command(MENU_ID_ACCOUNT)
        operation = self.complete()
        self.assertEqual(operation['state'], 'error')
        self.assertIn('模拟密码错误', operation['message'])
        self.assertEqual(self.portal.calls, ['prepare', 'logout', 'login'])

    def test_logout_command_disconnects(self):
        self.app.handle_command(MENU_ID_LOGOUT)
        operation = self.complete()
        self.assertEqual(operation['state'], 'success')
        self.assertEqual(self.portal.calls, ['prepare', 'logout'])
        self.assertIn('已确认当前设备下线', operation['message'])

    def test_tray_message_dispatch_decodes_version4_lparam(self):
        import tray as tray_module
        fired = []
        self.app.show_menu = lambda: fired.append('menu')
        self.app.open_panel = lambda: fired.append('panel')
        nid_uid = 1 << 16  # NOTIFYICON_VERSION_4 packs the icon uID here.
        # Version 4 encoding: right-click -> WM_CONTEXTMENU, left-click -> NIN_SELECT.
        self.app._wnd_proc(self.app.hwnd, tray_module.WM_TRAY, 0, nid_uid | 0x7B)
        self.app._wnd_proc(self.app.hwnd, tray_module.WM_TRAY, 0, nid_uid | tray_module.NIN_SELECT)
        # Legacy encoding: raw mouse messages with a zero high word.
        self.app._wnd_proc(self.app.hwnd, tray_module.WM_TRAY, 0, 0x0205)
        self.app._wnd_proc(self.app.hwnd, tray_module.WM_TRAY, 0, 0x0202)
        self.assertEqual(fired, ['menu', 'panel', 'menu', 'panel'])

    def test_autostart_command_toggles_and_notifies(self):
        with patch('tray.autostart_enabled', return_value=False), \
             patch('tray.set_autostart') as setter, \
             patch.object(self.app, 'notify') as notice:
            self.app.handle_command(MENU_ID_AUTOSTART)
        setter.assert_called_once_with(True)
        self.assertIn('已开启', notice.call_args.args[0])

    def test_quit_blocked_while_busy(self):
        original_status = self.portal.status

        def slow_status():
            time.sleep(.4)
            return original_status()

        self.portal.status = slow_status
        self.app.handle_command(MENU_ID_ACCOUNT)
        with patch('tray.messagebox.showinfo') as notice:
            self.app.quit()
        notice.assert_called_once()  # Refused with an explanation while the switch is running.
        self.assertFalse(self.app.closed)
        self.complete()
        self.app.quit()
        self.assertTrue(self.app.closed)


if __name__ == '__main__':
    unittest.main()

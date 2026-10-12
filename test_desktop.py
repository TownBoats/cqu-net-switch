import gc
import json
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from core import AppError, Profiles, export_profiles_text, now
from desktop import Desktop


class DesktopPortal:
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

    def online_devices(self):
        return [dict(account=self.account, ipv4='10.0.0.8', mac='AA:BB:CC:DD:EE:FF',
                     since='2026-10-11 18:05:20', seconds=348, alias='', bound=True,
                     perceive=False, owner=True)]


@unittest.skipUnless(os.name == 'nt', 'Windows desktop')
class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.profiles = Profiles(Path(self.directory.name) / 'profiles.json')
        self.profiles.save({'account': 'targetT', 'password': 'dummy-test-password'})
        self.root = tk.Tk()
        self.root.withdraw()
        self.portal = DesktopPortal()
        self.app = Desktop(self.root, self.profiles, self.portal, start_query=False)
        self.app.service.pause = lambda _: None
        self.app.status, self.app.stale = self.portal.status(), False
        self.app.render_status()

    def tearDown(self):
        self.app.closed = True
        for timer in self.root.tk.call('after', 'info'):
            self.root.after_cancel(timer)
        self.root.destroy()
        # Finalize Tk wrapper objects on the main thread; collecting them later
        # inside a worker thread aborts the process (Tcl_AsyncDelete).
        gc.collect()
        self.directory.cleanup()

    def complete(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.root.update()
            if self.app.last_operation and self.app.last_operation['state'] in ('success', 'error') and not self.app.querying:
                return
            time.sleep(.02)
        self.fail('Desktop operation did not finish')

    def test_actual_connect_button_runs_and_updates_ui(self):
        with patch('desktop.messagebox.askyesno', return_value=True):
            self.app.connect_button.invoke()
        self.complete()
        self.assertEqual(self.portal.calls, ['prepare', 'logout', 'login'])
        self.assertIn('targetT', self.app.identity.get())
        self.assertIn('已连接', self.app.message.get())

    def test_actual_logout_button_updates_offline_status(self):
        with patch('desktop.messagebox.askyesno', return_value=True):
            self.app.logout_button.invoke()
        self.complete()
        self.assertEqual(self.portal.calls, ['prepare', 'logout'])
        self.assertEqual(self.app.badge.get(), '未登录')
        self.assertIn('已确认当前设备下线', self.app.message.get())

    def test_double_click_account_switches_connection(self):
        self.assertIn('switch_selected', self.app.tree.bind('<Double-1>'))
        self.app.tree.selection_set(self.profiles.public()[0]['id'])
        with patch('desktop.messagebox.askyesno', return_value=True):
            self.app.switch_selected()
        self.complete()
        self.assertEqual(self.portal.calls, ['prepare', 'logout', 'login'])
        self.assertIn('已连接', self.app.message.get())

    def test_devices_tab_lists_online_terminals(self):
        self.app.refresh()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.app.querying:
            self.root.update()
            time.sleep(.02)
        self.assertFalse(self.app.querying)
        rows = self.app.devices_tree.get_children()
        self.assertEqual(len(rows), 1)
        values = self.app.devices_tree.item(rows[0])['values']
        self.assertEqual(values[0], 'AA:BB:CC:DD:EE:FF')
        self.assertEqual(values[3], '5 分钟')
        self.assertIn('本机', values[4])
        self.assertIn('MAC 绑定', values[4])
        self.assertIn('1 台终端在线', self.app.devices_title.get())

    def test_export_button_writes_encrypted_file(self):
        target = Path(self.directory.name) / 'export.json'
        with patch('desktop.simpledialog.askstring', side_effect=['pw-export', 'pw-export']), \
             patch('desktop.filedialog.asksaveasfilename', return_value=str(target)):
            self.app.export_button.invoke()
        text = target.read_text(encoding='utf-8')
        self.assertEqual(json.loads(text)['kind'], 'account-export')
        self.assertNotIn('dummy-test-password', text)
        self.assertIn('导出完成', self.app.message.get())

    def test_import_button_skips_existing_accounts(self):
        target = Path(self.directory.name) / 'export.json'
        target.write_text(export_profiles_text(
            [dict(account='targetT', label='已有', password='dummy-test-password'),
             dict(account='newT', label='', password='new-password')], 'pw'), encoding='utf-8')
        with patch('desktop.filedialog.askopenfilename', return_value=str(target)), \
             patch('desktop.simpledialog.askstring', return_value='pw'):
            self.app.import_button.invoke()
        accounts = sorted(p['account'] for p in self.profiles.public())
        self.assertEqual(accounts, ['newT', 'targetT'])  # targetT already existed, skipped.
        self.assertIn('已导入 1 个账号', self.app.message.get())
        self.assertIn('1 个本机已存在跳过', self.app.message.get())
        # Imported password must be usable (DPAPI roundtrip for the current user).
        item = next(p for p in self.profiles.public() if p['account'] == 'newT')
        from core import protect
        self.assertEqual(protect(self.profiles.get(item['id'])['secret'], True), 'new-password')

    def test_failed_login_message_survives_auto_refresh(self):
        self.portal.fail_login = True
        with patch('desktop.messagebox.askyesno', return_value=True):
            self.app.connect_button.invoke()
        self.complete()
        self.assertIn('模拟密码错误', self.app.message.get())
        self.app.refresh()
        self.complete()
        self.assertIn('模拟密码错误', self.app.message.get())
        self.assertIn('模拟密码错误', self.app.log_text.get('1.0', 'end'))


if __name__ == '__main__':
    unittest.main()

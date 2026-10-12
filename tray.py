"""Windows tray app: lives in the notification area; right-click switches accounts.

Zero third-party dependencies: the tray icon, popup menu and balloon notices are
driven through Shell_NotifyIconW / TrackPopupMenu via ctypes. Tk supplies the
on-demand management panel (the Desktop window) and the timer loop that pumps
the hidden tray window's messages, so everything stays on one thread.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox

from core import AppError, Dashboard, Profiles
from desktop import Desktop, data_path, migrate_profiles

APP = '山城连接'
TIP_LIMIT = 127
AUTO_REFRESH_MS = 30_000
POLL_MS = 400
MENU_ID_REFRESH = 100
MENU_ID_LOGOUT = 101
MENU_ID_PANEL = 102
MENU_ID_QUIT = 103
MENU_ID_AUTOSTART = 104
MENU_ID_ACCOUNT = 1000

if os.name == 'nt':
    import winreg
    _u = ctypes.WinDLL('user32', use_last_error=True)
    _g = ctypes.WinDLL('gdi32', use_last_error=True)
    _s = ctypes.WinDLL('shell32', use_last_error=True)
    _k = ctypes.WinDLL('kernel32', use_last_error=True)

    HANDLE = ctypes.c_void_p
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HANDLE, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [('style', wintypes.UINT), ('lpfnWndProc', WNDPROC),
                    ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                    ('hInstance', HANDLE), ('hIcon', HANDLE), ('hCursor', HANDLE),
                    ('hbrBackground', HANDLE), ('lpszMenuName', wintypes.LPCWSTR),
                    ('lpszClassName', wintypes.LPCWSTR)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('hWnd', HANDLE), ('uID', wintypes.UINT),
                    ('uFlags', wintypes.UINT), ('uCallbackMessage', wintypes.UINT),
                    ('hIcon', HANDLE), ('szTip', wintypes.WCHAR * 128),
                    ('dwState', wintypes.DWORD), ('dwStateMask', wintypes.DWORD),
                    ('szInfo', wintypes.WCHAR * 256), ('uTimeoutOrVersion', wintypes.UINT),
                    ('szInfoTitle', wintypes.WCHAR * 64), ('dwInfoFlags', wintypes.DWORD),
                    ('guidItem', ctypes.c_byte * 16), ('hBalloonIcon', HANDLE)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [('fIcon', wintypes.BOOL), ('xHotspot', wintypes.DWORD),
                    ('yHotspot', wintypes.DWORD), ('hbmMask', HANDLE), ('hbmColor', HANDLE)]

    WM_NULL, WM_DESTROY, WM_USER = 0x0000, 0x0002, 0x0400
    WM_CONTEXTMENU = 0x007B
    WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
    WM_TRAY = WM_USER + 1
    NIN_SELECT = WM_USER  # NOTIFYICON_VERSION_4 left-click notification code.
    PM_REMOVE = 0x0001
    NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
    NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 0x1, 0x2, 0x3
    MF_ENABLED, MF_GRAYED, MF_CHECKED, MF_SEPARATOR = 0x0, 0x1, 0x8, 0x800
    TPM_RETURNCMD, TPM_NONOTIFY, TPM_RIGHTBUTTON = 0x100, 0x80, 0x2
    SM_CXSMICON, SM_CYSMICON = 49, 50
    WHITE_BRUSH, BLACK_BRUSH = 0, 4
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010

    _u.DefWindowProcW.argtypes = [HANDLE, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    _u.DefWindowProcW.restype = LRESULT
    _u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    _u.RegisterClassW.restype = wintypes.ATOM if hasattr(wintypes, 'ATOM') else wintypes.WORD
    _u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   HANDLE, HANDLE, HANDLE, HANDLE]
    _u.CreateWindowExW.restype = HANDLE
    _u.DestroyWindow.argtypes = [HANDLE]
    _u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), HANDLE, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    _u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _u.DispatchMessageW.restype = LRESULT
    _u.PostMessageW.argtypes = [HANDLE, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    _u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _u.SetForegroundWindow.argtypes = [HANDLE]
    _u.CreatePopupMenu.restype = HANDLE
    _u.AppendMenuW.argtypes = [HANDLE, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    _u.TrackPopupMenu.argtypes = [HANDLE, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, HANDLE, HANDLE]
    _u.DestroyMenu.argtypes = [HANDLE]
    _u.GetSystemMetrics.argtypes = [ctypes.c_int]
    _u.GetDC.argtypes = [HANDLE]
    _u.GetDC.restype = HANDLE
    _u.ReleaseDC.argtypes = [HANDLE, HANDLE]
    _u.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
    _u.CreateIconIndirect.restype = HANDLE
    _u.LoadImageW.argtypes = [HANDLE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _u.LoadImageW.restype = HANDLE
    _u.DestroyIcon.argtypes = [HANDLE]
    _u.FillRect.argtypes = [HANDLE, ctypes.POINTER(wintypes.RECT), HANDLE]
    _g.CreateCompatibleDC.argtypes = [HANDLE]
    _g.CreateCompatibleDC.restype = HANDLE
    _g.CreateCompatibleBitmap.argtypes = [HANDLE, ctypes.c_int, ctypes.c_int]
    _g.CreateCompatibleBitmap.restype = HANDLE
    _g.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, HANDLE]
    _g.CreateBitmap.restype = HANDLE
    _g.SelectObject.argtypes = [HANDLE, HANDLE]
    _g.SelectObject.restype = HANDLE
    _g.DeleteObject.argtypes = [HANDLE]
    _g.DeleteDC.argtypes = [HANDLE]
    _g.CreateSolidBrush.argtypes = wintypes.DWORD,
    _g.CreateSolidBrush.restype = HANDLE
    _g.GetStockObject.argtypes = [ctypes.c_int]
    _g.GetStockObject.restype = HANDLE
    _g.Ellipse.argtypes = [HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    _s.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    _s.Shell_NotifyIconW.restype = wintypes.BOOL
    _k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _k.GetModuleHandleW.restype = HANDLE


def rgb(r, g, b):
    return r | (g << 8) | (b << 16)


RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_VALUE = 'CQU-Net-Switch'


def launch_command():
    """The command line Windows should run at logon for this app."""
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}"'
    script = Path(__file__).resolve()
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{interpreter}" "{script}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
        return value == launch_command()
    except OSError:
        return False


def set_autostart(enable):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enable:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


COLORS = {'online': rgb(0x2e, 0x9e, 0x5b), 'offline': rgb(0x9a, 0xa5, 0x9b), 'busy': rgb(0xd9, 0x8e, 0x32)}


def resource_base():
    """Bundled asset directory: PyInstaller's extraction dir when frozen."""
    return Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'assets'


def make_circle_icon(colorref, size):
    """Draw a filled circle as an HICON; returns 0 on failure."""
    screen = _u.GetDC(None)
    if not screen:
        return None
    color_dc = _g.CreateCompatibleDC(screen)
    mask_dc = _g.CreateCompatibleDC(screen)
    color_bmp = _g.CreateCompatibleBitmap(screen, size, size)
    mask_bmp = _g.CreateBitmap(size, size, 1, 1, None)
    old_color = _g.SelectObject(color_dc, color_bmp)
    old_mask = _g.SelectObject(mask_dc, mask_bmp)
    rect = wintypes.RECT(0, 0, size, size)
    try:
        _u.FillRect(mask_dc, ctypes.byref(rect), _g.GetStockObject(WHITE_BRUSH))
        _g.SelectObject(mask_dc, _g.GetStockObject(BLACK_BRUSH))
        _g.Ellipse(mask_dc, 1, 1, size - 1, size - 1)
        _u.FillRect(color_dc, ctypes.byref(rect), _g.GetStockObject(BLACK_BRUSH))
        brush = _g.CreateSolidBrush(colorref)
        _g.SelectObject(color_dc, brush)
        _g.Ellipse(color_dc, 1, 1, size - 1, size - 1)
        _g.SelectObject(color_dc, old_color)
        _g.SelectObject(mask_dc, old_mask)
        _g.DeleteObject(brush)
        info = ICONINFO(True, 0, 0, mask_bmp, color_bmp)
        return _u.CreateIconIndirect(ctypes.byref(info))
    finally:
        _g.DeleteObject(color_bmp)
        _g.DeleteObject(mask_bmp)
        _g.DeleteDC(color_dc)
        _g.DeleteDC(mask_dc)
        _u.ReleaseDC(None, screen)


def tooltip_text(status, stale, busy, message):
    if busy:
        return f'{APP} · {message}'[:TIP_LIMIT]
    if stale or not status:
        return f'{APP} · 状态未知（右键刷新）'
    if status['online']:
        who = '  '.join(filter(None, [status.get('name'), status.get('account')])) or '已连接'
        return f'{APP} · {who}'[:TIP_LIMIT]
    return f'{APP} · 未登录（右键选择账号连接）'


def icon_state(status, stale, busy):
    if busy:
        return 'busy'
    if not stale and status and status['online']:
        return 'online'
    return 'offline'


def build_menu(status, stale, busy, message, profiles, autostart=False):
    """Pure menu model: list of entries, each a separator or a clickable item."""
    if busy:
        header = message or '正在处理连接…'
    elif stale or not status:
        header = '状态未知 · 查询失败不代表已下线'
    elif status['online']:
        who = '  ·  '.join(filter(None, [status.get('name'), status.get('account')]))
        header = f'已连接：{who}' if who else '校园网已连接'
    else:
        header = '未登录 · 选择账号开始连接'
    entries = [dict(text=header, enabled=False)]
    if profiles:
        entries.append(dict(separator=True))
        current = '' if stale or not status or not status['online'] else status['account']
        for index, p in enumerate(profiles):
            label = f"{p['label']}（{p['account']}）" if p['label'] != p['account'] else p['account']
            entries.append(dict(id=MENU_ID_ACCOUNT + index, text=label,
                                checked=p['account'] == current, enabled=not busy))
    else:
        entries.append(dict(text='（尚未保存账号，请在管理面板添加）', enabled=False))
    entries.append(dict(separator=True))
    entries.append(dict(id=MENU_ID_REFRESH, text='刷新状态', enabled=not busy))
    online = not busy and not stale and status and status['online']
    entries.append(dict(id=MENU_ID_LOGOUT, text='断开当前连接', enabled=bool(online)))
    entries.append(dict(separator=True))
    entries.append(dict(id=MENU_ID_PANEL, text='管理面板 · 账号与记录', enabled=True))
    entries.append(dict(id=MENU_ID_AUTOSTART, text='开机自动启动', checked=autostart, enabled=not busy))
    entries.append(dict(id=MENU_ID_QUIT, text='退出', enabled=not busy))
    return entries


class TrayApp:
    def __init__(self, root, profiles, service):
        self.root = root
        self.profiles = profiles
        self.service = service
        self.status = None
        self.stale = True
        self.querying = False
        self.closed = False
        self.panel = None
        self.results = queue.Queue()
        self.last_operation = dict(self.service.snapshot()['operation'])
        self.hwnd = None
        self.nid = None
        self.icons = {}
        self.state = 'offline'
        self._wnd_proc_ref = WNDPROC(self._wnd_proc)

    # ---- Win32 tray plumbing -------------------------------------------------

    def start(self):
        width = max(16, _u.GetSystemMetrics(SM_CXSMICON))
        height = max(16, _u.GetSystemMetrics(SM_CYSMICON))
        size = max(width, height)
        assets = resource_base()
        self.icons = {}
        for name, color in COLORS.items():
            icon = None
            path = assets / f'tray-{name}.ico'
            if path.is_file():
                icon = _u.LoadImageW(None, str(path), IMAGE_ICON, width, height, LR_LOADFROMFILE)
            if not icon:  # Fall back to a plain status dot if assets are missing.
                icon = make_circle_icon(color, size)
            self.icons[name] = icon
        if not all(self.icons.values()):
            raise AppError('无法创建托盘图标。')
        instance = _k.GetModuleHandleW(None)
        wc = WNDCLASSW(0, self._wnd_proc_ref, 0, 0, instance, None, None, None, None, 'CQUNetSwitchTray')
        if not _u.RegisterClassW(ctypes.byref(wc)) and ctypes.get_last_error() != 1410:
            raise AppError('无法注册托盘窗口。')
        self.hwnd = _u.CreateWindowExW(0, 'CQUNetSwitchTray', APP, 0, 0, 0, 0, 0, None, None, instance, None)
        if not self.hwnd:
            raise AppError('无法创建托盘窗口。')
        self.nid = NOTIFYICONDATAW()
        self.nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        self.nid.hWnd = self.hwnd
        self.nid.uID = 1
        self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self.nid.uCallbackMessage = WM_TRAY
        self.nid.hIcon = self.icons['offline']
        self.nid.szTip = f'{APP} · 正在查询状态…'
        if not _s.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.nid)):
            raise AppError('无法添加托盘图标。')
        version = NOTIFYICONDATAW()
        version.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        version.hWnd = self.hwnd
        version.uID = 1
        version.uTimeoutOrVersion = 4
        _s.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(version))
        self.root.after(30, self.pump_messages)
        self.root.after(POLL_MS, self.poll)
        self.root.after(AUTO_REFRESH_MS, self.auto_tick)
        self.refresh()

    def pump_messages(self):
        if self.closed or not self.hwnd:
            return
        msg = wintypes.MSG()
        while _u.PeekMessageW(ctypes.byref(msg), self.hwnd, 0, 0, PM_REMOVE):
            _u.TranslateMessage(ctypes.byref(msg))
            _u.DispatchMessageW(ctypes.byref(msg))
        self.root.after(30, self.pump_messages)

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                # Version 4 icons pack the icon uID into the high word of
                # lParam (notification code in the low word); older behavior
                # passes the raw mouse message with a zero high word.
                # Decoding with LOWORD covers both families.
                code = lparam & 0xFFFF
                if code in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self.show_menu()
                elif code in (WM_LBUTTONUP, WM_LBUTTONDBLCLK, NIN_SELECT):
                    self.open_panel()
                return 0
        except Exception:
            pass
        return _u.DefWindowProcW(hwnd, msg, wparam, lparam)

    def update_icon(self):
        state = icon_state(self.status, self.stale, self.busy())
        tip = tooltip_text(self.status, self.stale, self.busy(), self.operation_message())
        if state == self.state and getattr(self, '_tip', None) == tip:
            return
        self.state, self._tip = state, tip
        self.nid.uFlags = NIF_ICON | NIF_TIP
        self.nid.hIcon = self.icons[state]
        self.nid.szTip = tip
        _s.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    def notify(self, message, error=False):
        self.nid.uFlags = NIF_INFO
        self.nid.szInfo = message[:255]
        self.nid.szInfoTitle = APP
        self.nid.dwInfoFlags = NIIF_ERROR if error else NIIF_INFO
        self.nid.uTimeoutOrVersion = 5000
        _s.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    def show_menu(self):
        entries = build_menu(self.status, self.stale, self.busy(), self.operation_message(),
                             self.profiles.public(), autostart=autostart_enabled())
        menu = _u.CreatePopupMenu()
        if not menu:
            return
        for entry in entries:
            if entry.get('separator'):
                _u.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                continue
            flags = 0
            if not entry.get('enabled', True):
                flags |= MF_GRAYED
            if entry.get('checked'):
                flags |= MF_CHECKED
            _u.AppendMenuW(menu, flags, entry.get('id', 0), entry['text'])
        point = wintypes.POINT()
        _u.GetCursorPos(ctypes.byref(point))
        _u.SetForegroundWindow(self.hwnd)
        command = _u.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON,
                                    point.x, point.y, 0, self.hwnd, None)
        _u.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        _u.DestroyMenu(menu)
        if command:
            self.handle_command(command)

    def handle_command(self, command):
        if command == MENU_ID_REFRESH:
            self.refresh(force=True)
        elif command == MENU_ID_LOGOUT:
            self.start_action('logout')
        elif command == MENU_ID_PANEL:
            self.open_panel()
        elif command == MENU_ID_AUTOSTART:
            self.toggle_autostart()
        elif command == MENU_ID_QUIT:
            self.quit()
        elif command >= MENU_ID_ACCOUNT:
            profiles = self.profiles.public()
            index = command - MENU_ID_ACCOUNT
            if 0 <= index < len(profiles):
                self.start_action('switch', profiles[index]['id'])

    # ---- state and operations -------------------------------------------------

    def busy(self):
        return self.service.snapshot()['operation']['state'] == 'running'

    def operation_message(self):
        return self.service.snapshot()['operation'].get('message', '')

    def refresh(self, force=False):
        if self.closed or self.querying or (self.busy() and not force):
            return
        self.querying = True

        def work():
            try:
                self.results.put((self.service.portal.status(), None))
            except Exception as error:
                message = str(error) if isinstance(error, AppError) else '查询失败，请检查网络后重试。'
                self.results.put((None, message))

        threading.Thread(target=work, daemon=True).start()

    def auto_tick(self):
        if not self.closed:
            self.refresh()
            self.root.after(AUTO_REFRESH_MS, self.auto_tick)

    def poll(self):
        if self.closed:
            return
        try:
            while True:
                status, error = self.results.get_nowait()
                self.querying = False
                if error:
                    self.stale = True
                else:
                    self.status, self.stale = status, False
        except queue.Empty:
            pass
        operation = self.service.snapshot()['operation']
        if operation != self.last_operation:
            self.last_operation = dict(operation)
            # Fire on every transition INTO a finished state, even if the whole
            # operation fit between two polls and "running" was never observed.
            if operation['state'] in ('success', 'error'):
                self.notify(operation['message'], error=operation['state'] == 'error')
                self.refresh(force=True)
        self.update_icon()
        self.root.after(POLL_MS, self.poll)

    def toggle_autostart(self):
        try:
            enable = not autostart_enabled()
            set_autostart(enable)
            self.notify('已开启开机自动启动。' if enable else '已关闭开机自动启动。')
        except OSError:
            self.notify('无法修改开机启动项，请检查注册表权限。', error=True)

    def start_action(self, action, profile_id=None):
        try:
            self.service.start(action, profile_id)
            self.stale = True
            self.update_icon()
        except Exception as error:
            message = str(error) if isinstance(error, AppError) else '无法读取账号信息，请重新保存密码。'
            self.notify(message, error=True)

    # ---- management panel ------------------------------------------------------

    def open_panel(self):
        if self.closed:
            return
        if self.panel is None:
            self.panel = Desktop(self.root, self.profiles, self.service.portal, start_query=False)
            # Share one Dashboard so tray menu and panel never run competing operations.
            self.panel.service = self.service
            self.panel.auto.set(False)  # The tray owns periodic refresh.
            self.panel.close = self.hide_panel
            self.root.protocol('WM_DELETE_WINDOW', self.hide_panel)
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.panel.refresh()

    def hide_panel(self):
        if self.busy():
            messagebox.showinfo('正在切换连接', '请等待当前操作完成后再关闭窗口。', parent=self.root)
            return
        self.root.withdraw()

    # ---- shutdown ----------------------------------------------------------------

    def quit(self, force=False):
        if self.closed:
            return
        if not force and self.busy():
            messagebox.showinfo('正在切换连接', '请等待当前操作完成后再退出。', parent=self.root if self.panel else None)
            return
        self.closed = True
        if self.hwnd and self.nid:
            _s.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
        for icon in self.icons.values():
            if icon:
                _u.DestroyIcon(icon)
        if self.hwnd:
            _u.DestroyWindow(self.hwnd)
            self.hwnd = None
        try:
            for timer in self.root.tk.call('after', 'info'):
                self.root.after_cancel(timer)
        except tk.TclError:
            pass
        self.root.destroy()


def self_test(report_path):
    """Build and tear down the real tray icon and menu model without touching the network."""
    report = {'ok': False, 'frozen': bool(getattr(sys, 'frozen', False)), 'checks': []}
    root = None
    app = None
    try:
        with tempfile.TemporaryDirectory(prefix='cqu-tray-smoke-') as directory:
            profiles = Profiles(Path(directory) / 'profiles.json')
            profiles.save({'account': 'smokeT', 'label': '打包测试', 'password': 'test-only-password'})
            root = tk.Tk()
            root.withdraw()
            app = TrayApp(root, profiles, Dashboard(profiles))
            app.start()
            for _ in range(5):
                root.update()
            assert app.hwnd and all(app.icons.values())
            report['checks'].append('Tray icon added and removed via Shell_NotifyIconW')
            entries = build_menu(app.status, app.stale, False, '', profiles.public())
            account_items = [e for e in entries if e.get('id', 0) >= MENU_ID_ACCOUNT]
            assert len(account_items) == 1 and 'smokeT' in account_items[0]['text']
            assert any(e.get('id') == MENU_ID_QUIT for e in entries)
            report['checks'].append('Menu model lists saved account and controls')
            online = dict(online=True, account='smokeT', name='测试', ipv4='10.0.0.8',
                          ipv6='', mac='', minutes=1, bytes=0, checkedAt='')
            tip = tooltip_text(online, False, False, '')
            assert 'smokeT' in tip and len(tip) <= TIP_LIMIT
            report['checks'].append('Tooltip text within the 127-char tray limit')
            app.quit(force=True)
            app = None
            report['ok'] = True
    except Exception as error:
        report['error'] = type(error).__name__
    finally:
        if app is not None:
            app.quit(force=True)
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        Path(report_path).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-test', metavar='REPORT')
    args = parser.parse_args()
    if os.name != 'nt':
        raise SystemExit('托盘版仅支持 Windows。')
    if args.self_test:
        self_test(args.self_test)
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    root.withdraw()
    mutex = None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    try:
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        mutex = kernel.CreateMutexW(None, False, 'Local\\CQU-Net-Switch-Desktop')
        if not mutex:
            raise AppError('无法初始化托盘程序。')
        if ctypes.get_last_error() == 183:
            messagebox.showinfo(APP, '程序已经运行，请查看右下角托盘图标。', parent=root)
            return
        path = data_path()
        migrate_profiles(path)
        profiles = Profiles(path)
        app = TrayApp(root, profiles, Dashboard(profiles))
        app.start()
        root.mainloop()
    except Exception as error:
        messagebox.showerror('无法启动', str(error) if isinstance(error, AppError)
                             else '无法加载账号数据或创建托盘图标，请检查本机环境。', parent=root)
    finally:
        if mutex:
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(mutex)


if __name__ == '__main__':
    main()

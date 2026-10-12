"""Native Tk desktop app; no browser, webview, localhost server or third-party UI."""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from core import AppError, Dashboard, Profiles, export_profiles_text, parse_profiles_import

BG = '#f5f5ef'
INK = '#243e35'
MUTED = '#6b776d'
GREEN = '#315e48'


def data_path():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'CQU-Net-Switch' / 'profiles.json'


def migrate_profiles(destination):
    """Copy the earlier web app's store once, preserving the original."""
    if destination.exists():
        return
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    candidates = [base / 'data' / 'profiles.json']
    if base.name == 'dist':
        candidates.append(base.parent / 'data' / 'profiles.json')
    for source in candidates:
        if source.is_file():
            Profiles(source)  # Validate before preserving the encrypted records.
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            return


def format_duration(seconds):
    if seconds is None:
        return '—'
    if seconds < 60:
        return f'{seconds} 秒'
    hours, minutes = seconds // 3600, seconds % 3600 // 60
    return f'{hours} 小时 {minutes} 分' if hours else f'{minutes} 分钟'


class Desktop:
    def __init__(self, root, profiles, portal=None, start_query=True):
        self.root = root
        self.service = Dashboard(profiles, portal)
        self.results = queue.Queue()
        self.status = None
        self.devices = None
        self.stale = True
        self.querying = False
        self.busy = False
        self.closed = False
        self.generation = 0
        self.last_operation = None
        self.last_events = None
        self.operation_notice = ''
        self.editor = None
        root.title('山城连接 · 重庆大学校园网')
        try:
            icon = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'assets' / 'logo.ico'
            if icon.is_file():
                root.iconbitmap(default=str(icon))
        except tk.TclError:
            pass
        scale = max(1.0, root.winfo_fpixels('1i') / 96)
        width = min(round(820 * scale), round(root.winfo_screenwidth() * .9))
        height = min(round(720 * scale), round(root.winfo_screenheight() * .88))
        root.geometry(f'{width}x{height}')
        root.minsize(min(width, round(680 * scale)), min(height, round(600 * scale)))
        root.configure(bg=BG)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.style()
        self.build()
        self.render_profiles()
        self.root.after(120, self.pump)
        self.root.after(15000, self.auto_tick)
        if start_query:
            self.refresh()

    def style(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background=BG, foreground=INK)
        style.configure('TButton', padding=(12, 7), background='#e7ecdf', borderwidth=0)
        style.map('TButton', background=[('active', '#d8e2cf')], foreground=[('disabled', '#9aa595')])
        style.configure('Primary.TButton', background=GREEN, foreground='#f9faef')
        style.map('Primary.TButton', background=[('active', '#234b36'), ('disabled', '#a6b39f')], foreground=[('disabled', '#f0f1e8')])
        style.configure('Muted.TLabel', foreground=MUTED)
        style.configure('TEntry', padding=7, fieldbackground='#fffef8')
        style.configure('Treeview', rowheight=37, fieldbackground='#fffef8', background='#fffef8', borderwidth=0)
        style.configure('Treeview.Heading', padding=(8, 9), background='#e7ecdf', font=('Microsoft YaHei UI', 9))
        style.map('Treeview', background=[('selected', '#d7e4cf')], foreground=[('selected', INK)])
        style.configure('TNotebook', borderwidth=0)
        style.configure('TNotebook.Tab', padding=(16, 8), background='#e7ecdf')

    def build(self):
        shell = ttk.Frame(self.root, padding=24)
        shell.pack(fill='both', expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(4, weight=1)
        header = ttk.Frame(shell)
        header.grid(row=0, column=0, sticky='ew')
        ttk.Label(header, text='山城连接', font=('Microsoft YaHei UI', 22, 'bold')).pack(side='left')
        ttk.Label(header, text='重庆大学校园网  /  桌面版 v1.1', style='Muted.TLabel').pack(side='left', padx=18)
        self.refresh_button = ttk.Button(header, text='刷新状态', command=self.refresh)
        self.refresh_button.pack(side='right')
        board = tk.Frame(shell, bg='#e8efdf', padx=20, pady=17)
        board.grid(row=1, column=0, sticky='ew', pady=(20, 16))
        top = tk.Frame(board, bg='#e8efdf')
        top.pack(fill='x')
        self.badge = tk.StringVar(value='正在查询')
        tk.Label(top, textvariable=self.badge, bg='#e8efdf', fg=GREEN, font=('Microsoft YaHei UI', 10)).pack(side='left')
        self.updated = tk.StringVar(value='尚未更新')
        tk.Label(top, textvariable=self.updated, bg='#e8efdf', fg=MUTED, font=('Microsoft YaHei UI', 9)).pack(side='right')
        self.identity = tk.StringVar(value='正在读取网络状态…')
        tk.Label(board, textvariable=self.identity, bg='#e8efdf', fg=INK, anchor='w', font=('Microsoft YaHei UI', 18, 'bold')).pack(fill='x', pady=(10, 6))
        self.device = tk.StringVar(value='IPv4  —    ·    MAC  —')
        tk.Label(board, textvariable=self.device, bg='#e8efdf', fg=MUTED, anchor='w', font=('Microsoft YaHei UI', 9)).pack(fill='x')
        self.metrics = tk.StringVar(value='门户累计流量  —    /    累计时长  —')
        ttk.Label(shell, textvariable=self.metrics).grid(row=2, column=0, sticky='w')
        options = ttk.Frame(shell)
        options.grid(row=3, column=0, sticky='ew', pady=(9, 14))
        self.auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text='每 15 秒自动刷新', variable=self.auto).pack(side='left')
        self.logout_button = ttk.Button(options, text='断开当前连接', command=lambda: self.start_action('logout'))
        self.logout_button.pack(side='right')
        self.tabs = ttk.Notebook(shell)
        self.tabs.grid(row=4, column=0, sticky='nsew')
        accounts = ttk.Frame(self.tabs, padding=(0, 12))
        accounts.columnconfigure(0, weight=1)
        accounts.rowconfigure(1, weight=1)
        devices_tab = ttk.Frame(self.tabs, padding=(0, 12))
        devices_tab.columnconfigure(0, weight=1)
        devices_tab.rowconfigure(1, weight=1)
        logs = ttk.Frame(self.tabs, padding=(0, 12))
        self.tabs.add(accounts, text='账号管理')
        self.tabs.add(devices_tab, text='在线终端')
        self.tabs.add(logs, text='操作记录')
        toolbar = ttk.Frame(accounts)
        toolbar.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        self.add_button = ttk.Button(toolbar, text='＋ 添加账号', command=self.open_editor)
        self.add_button.pack(side='left')
        self.edit_button = ttk.Button(toolbar, text='编辑', command=lambda: self.open_editor(self.selected()))
        self.edit_button.pack(side='left', padx=8)
        self.delete_button = ttk.Button(toolbar, text='移除', command=self.delete_profile)
        self.delete_button.pack(side='left')
        self.import_button = ttk.Button(toolbar, text='导入', command=self.import_profiles)
        self.import_button.pack(side='left', padx=(8, 0))
        self.export_button = ttk.Button(toolbar, text='导出', command=self.export_profiles)
        self.export_button.pack(side='left', padx=8)
        self.connect_button = ttk.Button(toolbar, text='连接所选账号', style='Primary.TButton', command=lambda: self.start_action('switch'))
        self.connect_button.pack(side='right')
        table_frame = ttk.Frame(accounts)
        table_frame.grid(row=1, column=0, sticky='nsew')
        self.tree = ttk.Treeview(table_frame, columns=('label', 'account', 'state'), show='headings', selectmode='browse', height=5)
        for key, label, width in [('label', '账号备注', 185), ('account', '校园网账号', 240), ('state', '状态', 100)]:
            self.tree.heading(key, text=label, anchor='w')
            self.tree.column(key, width=width, minwidth=80, anchor='w')
        scroll = ttk.Scrollbar(table_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', lambda _: self.controls())
        self.tree.bind('<Double-1>', self.switch_selected)
        self.hint = tk.StringVar()
        ttk.Label(accounts, textvariable=self.hint, style='Muted.TLabel', wraplength=710).grid(row=2, column=0, sticky='w', pady=(10, 0))
        self.devices_title = tk.StringVar(value='在线终端')
        ttk.Label(devices_tab, textvariable=self.devices_title, font=('Microsoft YaHei UI', 10, 'bold')).grid(row=0, column=0, sticky='w', pady=(0, 10))
        devices_frame = ttk.Frame(devices_tab)
        devices_frame.grid(row=1, column=0, sticky='nsew')
        self.devices_tree = ttk.Treeview(devices_frame, columns=('mac', 'ipv4', 'since', 'duration', 'flags'), show='headings', height=5)
        for key, label, width in [('mac', '设备 MAC', 185), ('ipv4', 'IPv4', 130), ('since', '上线时间', 165), ('duration', '在线时长', 95), ('flags', '标记', 130)]:
            self.devices_tree.heading(key, text=label, anchor='w')
            self.devices_tree.column(key, width=width, minwidth=70, anchor='w')
        devices_scroll = ttk.Scrollbar(devices_frame, orient='vertical', command=self.devices_tree.yview)
        self.devices_tree.configure(yscrollcommand=devices_scroll.set)
        self.devices_tree.pack(side='left', fill='both', expand=True)
        devices_scroll.pack(side='right', fill='y')
        self.devices_hint = tk.StringVar(value='随状态刷新自动更新。显示当前账号在全部设备上的在线情况，不只本机。')
        ttk.Label(devices_tab, textvariable=self.devices_hint, style='Muted.TLabel', wraplength=710).grid(row=2, column=0, sticky='w', pady=(10, 0))
        self.log_text = tk.Text(logs, wrap='word', bg='#fffef8', fg=INK, relief='flat', font=('Microsoft YaHei UI', 10), height=6, padx=12, pady=12, state='disabled')
        log_scroll = ttk.Scrollbar(logs, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side='left', fill='both', expand=True)
        log_scroll.pack(side='right', fill='y')
        self.message = tk.StringVar(value='密码仅保存在本机，由 Windows 账户加密保护。')
        self.progress_bar = ttk.Progressbar(shell, mode='indeterminate')
        self.progress_bar.grid(row=5, column=0, sticky='ew', pady=(9, 0))
        self.progress_bar.grid_remove()
        self.message_label = ttk.Label(shell, textvariable=self.message, style='Muted.TLabel', wraplength=720)
        self.message_label.grid(row=6, column=0, sticky='ew', pady=(13, 0))
        ttk.Label(shell, text='本机运行 · 非官方工具    /    关闭窗口不会注销校园网', style='Muted.TLabel', font=('Microsoft YaHei UI', 8)).grid(row=7, column=0, sticky='w', pady=(7, 0))

    def selected(self):
        selected = self.tree.selection()
        return selected[0] if selected else None

    def switch_selected(self, _event=None):
        """Double-click on an account row connects it directly."""
        if self.selected():
            self.start_action('switch')

    def controls(self):
        locked = self.busy or self.querying
        selected = self.selected()
        for button in (self.add_button, self.edit_button, self.delete_button, self.connect_button):
            disabled = locked or (button is not self.add_button and not selected)
            button.configure(state='disabled' if disabled else 'normal')
        for button in (self.import_button, self.export_button):
            button.configure(state='disabled' if locked else 'normal')
        self.refresh_button.configure(state='disabled' if locked else 'normal', text='查询中…' if self.querying else '刷新状态')
        self.logout_button.configure(state='normal' if not locked and not self.stale and self.status and self.status['online'] else 'disabled')

    def render_profiles(self):
        selected = self.selected()
        profiles = self.service.profiles.public()
        ids = {p['id'] for p in profiles}
        for item in self.tree.get_children():
            if item not in ids:
                self.tree.delete(item)
        for p in profiles:
            active = not self.stale and self.status and self.status['online'] and self.status['account'] == p['account']
            values = (p['label'], p['account'], '当前在线' if active else '已保存')
            if self.tree.exists(p['id']):
                self.tree.item(p['id'], values=values)
            else:
                self.tree.insert('', 'end', iid=p['id'], values=values)
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
        elif profiles:
            self.tree.selection_set(profiles[0]['id'])
        self.hint.set(f'已保存 {len(profiles)} 个账号。双击账号直接连接，切换时网络会短暂中断。' if profiles else '还没有保存的账号。点击“添加账号”，即可保存并切换常用连接。')
        self.controls()

    def render_status(self):
        if self.busy:
            self.badge.set('连接变更中')
            self.identity.set('正在更新校园网连接…')
        elif self.stale:
            self.badge.set('状态未知 · 查询失败不代表已下线')
            self.identity.set('暂时无法确认连接')
        elif self.status['online']:
            self.badge.set('● 校园网已连接')
            self.identity.set('  ·  '.join(filter(None, [self.status['name'], self.status['account']])))
        else:
            self.badge.set('未登录')
            self.identity.set('选择一个账号，开始连接')
        if self.status:
            checked = datetime.fromisoformat(self.status['checkedAt']).astimezone().strftime('%H:%M:%S')
            self.updated.set(('上次成功 ' if self.stale else '更新于 ') + checked)
            ipv6 = self.status.get('ipv6')
            self.device.set(f"IPv4  {self.status['ipv4'] or '—'}    ·    MAC  {self.status['mac'] or '—'}" + (f'\nIPv6  {ipv6}' if ipv6 and ipv6 != '::' else ''))
        if self.status and self.status['online'] and not self.stale:
            flow, minutes = self.status.get('bytes'), self.status.get('minutes')
            usage = f'{flow / 1024 ** 3:.2f} GB' if flow is not None else '—'
            duration = f'{minutes // 60} 小时 {minutes % 60} 分' if minutes is not None else '—'
            self.metrics.set(f'门户累计流量  {usage}    /    累计时长  {duration}')
        else:
            self.metrics.set('门户累计流量  —    /    累计时长  —')
        self.render_profiles()

    def render_devices(self):
        for item in self.devices_tree.get_children():
            self.devices_tree.delete(item)
        if not self.devices:
            self.devices_title.set('在线终端')
            self.devices_hint.set('当前未登录或在线终端查询失败；状态刷新成功后会自动更新。')
            return
        account = self.devices[0]['account'] or (self.status or {}).get('account', '')
        self.devices_title.set(f'账号 {account} · 共 {len(self.devices)} 台终端在线' if account else f'共 {len(self.devices)} 台终端在线')
        for index, d in enumerate(self.devices):
            flags = '  '.join(filter(None, ['本机' if d['owner'] else '',
                                            'MAC 绑定' if d['bound'] else '',
                                            '无感' if d['perceive'] else ''])) or '—'
            self.devices_tree.insert('', 'end', iid=str(index), values=(d['mac'] or '—', d['ipv4'] or '—',
                                                                        d['since'] or '—', format_duration(d['seconds']), flags))
        self.devices_hint.set('数据来自门户在线列表（只读）。切换账号只影响本机，其他终端保持在线。')

    def refresh(self):
        if self.busy or self.querying or self.closed:
            return
        self.querying = True
        generation = self.generation
        self.controls()
        def work():
            try:
                status = self.service.portal.status()
            except Exception as error:
                message = str(error) if isinstance(error, AppError) else '查询失败，请检查网络后重试。'
                self.results.put((generation, None, message, None))
                return
            try:
                devices = self.service.portal.online_devices()
            except Exception:
                devices = None  # Supplementary data; never fail the status refresh over it.
            self.results.put((generation, status, None, devices))
        threading.Thread(target=work, daemon=True).start()

    def auto_tick(self):
        if self.auto.get():
            self.refresh()
        self.root.after(15000, self.auto_tick)

    def pump(self):
        if self.closed:
            return
        try:
            while True:
                generation, state, error, devices = self.results.get_nowait()
                self.querying = False
                if generation == self.generation and not self.busy:
                    if error:
                        self.stale = True
                        if not self.operation_notice:
                            self.message.set(error)
                    else:
                        self.status, self.stale = state, False
                        self.devices = devices
                        if not self.operation_notice:
                            self.message.set('状态已更新。门户累计数据不代表本次连接用量。')
                    self.render_status()
                    self.render_devices()
        except queue.Empty:
            pass
        snapshot = self.service.snapshot()
        operation = snapshot['operation']
        if operation != self.last_operation:
            self.last_operation = operation
            was_busy = self.busy
            self.busy = operation['state'] == 'running'
            if operation['message']:
                self.operation_notice = operation['message']
                self.message.set(operation['message'])
                self.message_label.configure(foreground='#a94432' if operation['state'] == 'error' else GREEN)
            if self.busy:
                self.progress_bar.grid()
                if not was_busy:
                    self.progress_bar.start(12)
            else:
                self.progress_bar.stop()
                self.progress_bar.grid_remove()
            if was_busy and not self.busy:
                self.refresh()
            self.controls()
        if snapshot['events'] != self.last_events:
            self.last_events = snapshot['events']
            self.log_text.configure(state='normal')
            self.log_text.delete('1.0', 'end')
            for event in snapshot['events']:
                at = datetime.fromisoformat(event['at']).astimezone().strftime('%H:%M:%S')
                self.log_text.insert('end', f"{at}   {event['message']}\n\n")
            if not snapshot['events']:
                self.log_text.insert('end', '暂无操作记录。仅保留本次运行期间最近 40 条记录。')
            self.log_text.configure(state='disabled')
        self.root.after(120, self.pump)

    def start_action(self, action):
        if self.busy or self.querying:
            self.message.set('正在处理，请等待当前操作完成。' if self.busy else '正在查询状态，请稍后点击连接。')
            return
        profile_id = self.selected()
        if action == 'switch':
            if not profile_id:
                self.message.set('请先在列表中选择要连接的账号。')
                return
            profile = self.service.profiles.get(profile_id)
            prompt = f"连接账号 {profile['account']}？\n\n将按学校规则注销本机连接（包括解除本机 MAC 绑定），再登录目标账号。网络会短暂中断。"
        else:
            prompt = '确定断开当前校园网连接吗？\n\n按学校规则，同时解除本机 MAC 绑定以避免自动重连；其他设备不受影响。'
        if not messagebox.askyesno('确认连接操作', prompt, parent=self.root):
            return
        try:
            self.service.start(action, profile_id)
            self.generation += 1
            self.busy, self.stale = True, True
            self.operation_notice = '正在确认当前连接…'
            self.message.set(self.operation_notice)
            self.message_label.configure(foreground=GREEN)
            self.progress_bar.grid()
            self.progress_bar.start(12)
            self.render_status()
        except Exception as error:
            messagebox.showerror('无法开始操作', str(error) if isinstance(error, AppError) else '无法读取账号信息，请重新保存密码。', parent=self.root)

    def open_editor(self, profile_id=None):
        if self.busy:
            return
        if self.editor is not None and self.editor.winfo_exists():
            self.editor.lift()
            return
        p = self.service.profiles.get(profile_id) if profile_id else None
        win = self.editor = tk.Toplevel(self.root)
        win.title('编辑账号' if p else '添加账号')
        win.transient(self.root)
        win.resizable(False, False)
        win.configure(bg=BG)
        frame = ttk.Frame(win, padding=24)
        frame.pack(fill='both', expand=True)
        defaults = [p['label'] if p else (self.status or {}).get('name', ''), p['account'] if p else (self.status or {}).get('account', ''), '']
        fields = []
        for label, default, secret in zip(['账号备注（选填）', '校园网账号（保留后缀）', '密码（留空保留原密码）' if p else '登录密码'], defaults, [False, False, True]):
            ttk.Label(frame, text=label).pack(anchor='w', pady=(0, 5))
            entry = ttk.Entry(frame, width=37, show='●' if secret else '')
            entry.insert(0, default)
            entry.pack(fill='x', pady=(0, 14))
            fields.append(entry)
        ttk.Label(frame, text='密码由当前 Windows 账户加密，仅保存在本机。', style='Muted.TLabel', wraplength=370).pack(anchor='w', pady=(0, 12))
        def save():
            try:
                if self.busy:
                    raise AppError('请等待连接操作完成后再保存。')
                self.service.profiles.save(dict(id=profile_id, label=fields[0].get(), account=fields[1].get(), password=fields[2].get()))
                self.service.event('已保存账号设置。')
                fields[2].delete(0, 'end')
                win.destroy()
                self.editor = None
                self.render_profiles()
            except Exception as error:
                messagebox.showerror('保存失败', str(error) if isinstance(error, AppError) else '无法写入账号文件，请检查文件权限。', parent=win)
        ttk.Button(frame, text='保存账号', style='Primary.TButton', command=save).pack(fill='x')
        win.bind('<Return>', lambda _: save())
        win.bind('<Escape>', lambda _: win.destroy())
        win.grab_set()
        fields[2 if defaults[1] else 1].focus_set()

    def export_profiles(self):
        if self.busy:
            return
        if not self.service.profiles.public():
            self.message.set('还没有保存的账号，无需导出。')
            return
        password = simpledialog.askstring('导出账号', '设置导出密码（导入到新电脑时需要输入）。\n导出文件包含账号密码，请妥善保管：', show='●', parent=self.root)
        if not password:
            return
        confirm = simpledialog.askstring('导出账号', '再次输入导出密码确认：', show='●', parent=self.root)
        if confirm != password:
            messagebox.showerror('导出失败', '两次输入的密码不一致。', parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, title='保存账号导出文件',
                                            defaultextension='.json', initialfile='cqu-net-switch-accounts.json',
                                            filetypes=[('账号导出文件', '.json')])
        if not path:
            return
        try:
            text = export_profiles_text(self.service.profiles.export_items(), password)
            Path(path).write_text(text, encoding='utf-8')
            self.service.event('已导出账号到加密文件。')
            self.message.set('导出完成。文件由导出密码加密保护，导入后建议删除。')
        except Exception as error:
            messagebox.showerror('导出失败', str(error) if isinstance(error, AppError)
                                 else '无法写入导出文件，请检查路径权限。', parent=self.root)

    def import_profiles(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.root, title='选择账号导出文件',
                                          filetypes=[('账号导出文件', '.json'), ('所有文件', '*.*')])
        if not path:
            return
        password = simpledialog.askstring('导入账号', '输入导出时设置的密码：', show='●', parent=self.root)
        if password is None:
            return
        try:
            items = parse_profiles_import(Path(path).read_text(encoding='utf-8'), password)
            added, skipped = self.service.profiles.import_items(items)
            tail = f'，{skipped} 个本机已存在跳过' if skipped else ''
            self.service.event(f'导入完成：新增 {added} 个账号{tail}。')
            self.message.set(f'已导入 {added} 个账号{tail}。导入后建议删除导出文件。')
            self.render_profiles()
        except Exception as error:
            messagebox.showerror('导入失败', str(error) if isinstance(error, AppError)
                                 else '无法读取导入文件。', parent=self.root)

    def delete_profile(self):
        profile_id = self.selected()
        if not profile_id or self.busy:
            return
        if messagebox.askyesno('移除账号', '仅删除本机保存的账号信息，不会断开当前连接。继续？', parent=self.root):
            try:
                self.service.profiles.delete(profile_id)
                self.service.event('已移除本机保存的账号。')
                self.render_profiles()
            except Exception as error:
                messagebox.showerror('移除失败', str(error) if isinstance(error, AppError) else '无法写入账号文件。', parent=self.root)

    def close(self):
        if self.service.snapshot()['operation']['state'] == 'running':
            messagebox.showinfo('正在切换连接', '请等待当前操作完成后再关闭窗口。', parent=self.root)
            return
        self.closed = True
        self.root.destroy()


def self_test(report_path):
    """Exercise the packaged Tk/SSL/DPAPI runtime, never alter the real network."""
    from core import Portal, protect
    report = {'ok': False, 'frozen': bool(getattr(sys, 'frozen', False)), 'checks': []}
    root = None
    try:
        with tempfile.TemporaryDirectory(prefix='cqu-smoke-') as directory:
            profiles = Profiles(Path(directory) / 'profiles.json')
            profiles.save({'account': 'smokeT', 'label': '打包测试', 'password': 'test-only-password'})
            item = profiles.public()[0]
            assert protect(profiles.get(item['id'])['secret'], True) == 'test-only-password'
            report['checks'].append('DPAPI encrypted roundtrip')
            root = tk.Tk()
            root.withdraw()
            app = Desktop(root, profiles, start_query=False)
            root.update()
            assert len(app.tree.get_children()) == 1
            assert app.tree.item(item['id'])['values'][0] == '打包测试'
            report['checks'].append('Tk widgets and saved account list')
            # A read-only live request verifies packaged TLS and charset support.
            try:
                portal = Portal()
                live = portal.status()
                app.status, app.stale = live, False
                app.render_status()
                app.devices = portal.online_devices()
                app.render_devices()
                root.update()
                report['portal'] = 'online' if live['online'] else 'offline'
                report['devices'] = len(app.devices)
                report['checks'].append('Live HTTPS status and online-device list with UI render')
            except AppError:
                report['portal'] = 'unreachable'
            report['ok'] = True
    except Exception as error:
        report['error'] = type(error).__name__
    finally:
        if root is not None:
            root.destroy()
        Path(report_path).write_text(json.dumps(report, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-test', metavar='REPORT')
    args = parser.parse_args()
    if args.self_test:
        self_test(args.self_test)
        return
    if os.name == 'nt':
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    root.withdraw()
    mutex = None
    try:
        if os.name == 'nt':
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel.CreateMutexW.restype = ctypes.c_void_p
            mutex = kernel.CreateMutexW(None, False, 'Local\\CQU-Net-Switch-Desktop')
            if not mutex:
                raise AppError('无法初始化桌面程序。')
            if ctypes.get_last_error() == 183:
                messagebox.showinfo('山城连接', '程序已经运行，请从任务栏打开已有窗口。', parent=root)
                return
        path = data_path()
        migrate_profiles(path)
        Desktop(root, Profiles(path))
        root.deiconify()
        root.mainloop()
    except Exception as error:
        messagebox.showerror('无法启动', str(error) if isinstance(error, AppError) else '无法加载账号数据或创建窗口，请检查本机文件权限。', parent=root)
    finally:
        if mutex:
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(mutex)


if __name__ == '__main__':
    main()

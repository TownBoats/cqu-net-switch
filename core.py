"""Shared portal protocol, encrypted profiles and account switching. No UI or HTTP server."""
from __future__ import annotations
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import gzip
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import re
import socket
import ssl
import threading
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler, ProxyHandler
import uuid

PORTAL = 'https://login.cqu.edu.cn'

class AppError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def decode_jsonp(text):
    match = re.fullmatch(r'\s*(?:[\w.]+\s*\(\s*)?(\{.*\})(?:\s*\))?\s*;?\s*', text, re.S)
    if not match:
        raise AppError('认证门户返回了无法识别的状态，请稍后重试。')
    try:
        data = json.loads(match[1])
    except ValueError:
        raise AppError('认证门户返回的状态数据不完整。') from None
    if not isinstance(data, dict):
        raise AppError('认证门户未返回有效的数据。')
    return data


def parse_jsonp(text):
    data = decode_jsonp(text)
    if str(data.get('result')) not in ('0', '1'):
        raise AppError('认证门户未返回有效的在线状态。')
    return data


def number(value):
    try:
        return max(0, int(str(value).strip()))
    except (ValueError, TypeError):
        return None


def normalize_status(data):
    def string(key):
        return str(data.get(key) or '').strip()
    mac = string('olmac') or string('ss4')
    if re.fullmatch(r'[0-9a-fA-F]{12}', mac):
        mac = ':'.join(mac[i:i + 2] for i in range(0, 12, 2)).upper()
    flow = number(data.get('flow'))
    return dict(online=str(data['result']) == '1', account=string('uid') or string('AC'),
                name=string('NID'), ipv4=string('v4ip') or string('v46ip') or string('ss5'),
                ipv6=string('v6ip'), mac=mac, minutes=number(data.get('time')),
                bytes=flow * 1024 if flow is not None else None, checkedAt=now())


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credential-bearing requests to a redirect target.
        raise AppError('认证门户发生重定向，请打开官方认证页检查网络。')


class Portal:
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
        self.context = {}
        self.config = None
        self.context_lock = threading.RLock()

    def fetch(self, path, params=None):
        url = PORTAL + path
        if params:
            url += ('&' if '?' in url else '?') + urlencode(params)
        req = Request(url, headers={'User-Agent': 'CQU-Net-Dashboard/1.0',
                                    'Referer': PORTAL + '/', 'Accept-Encoding': 'gzip'})
        try:
            with self.opener.open(req, timeout=8) as response:
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise AppError('认证门户响应过大。')
                if response.headers.get('Content-Encoding') == 'gzip':
                    body = gzip.decompress(body)
                try:
                    return body.decode('utf-8-sig')
                except UnicodeDecodeError:
                    return body.decode('gb18030', errors='replace')
        except (URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError):
            # Exception strings may contain the credential-bearing upstream URL.
            raise AppError('无法连接认证门户，请确认本机已接入校园网，且系统时间和证书正常。') from None

    def status(self):
        data = parse_jsonp(self.fetch('/drcom/chkstatus', {'callback': 'cb', '_': time.time_ns()}))
        status = normalize_status(data)
        with self.context_lock:
            previous = self.context if self.context.get('ipv4') == status['ipv4'] else {}
            mac = re.sub(r'[:-]', '', status['mac']).upper()
            if not re.fullmatch(r'[0-9A-F]{12}', mac) or mac == '000000000000':
                mac = previous.get('mac', '')
            self.context = dict(ipv4=status['ipv4'], ipv6='' if status['ipv6'] == '::' else status['ipv6'],
                                mac=mac, account=status['account'], vlan=str(data.get('vlanid', 1)))
        return status

    def jsonp(self, path, params=None):
        return decode_jsonp(self.fetch(path, dict(params or {}, callback='cb', jsVersion='4.2.2', lang='zh', v=time.time_ns())))

    @staticmethod
    def check_result(data, action, password=''):
        if str(data.get('result')).lower() in ('1', 'ok'):
            return
        message = data.get('msg') or data.get('msga') or data.get('message') or '门户未返回成功状态'
        if isinstance(message, (dict, list)):
            message = '门户未返回成功状态'
        message = html.unescape(re.sub(r'<[^>]*>', '', str(message)))
        if password:
            message = message.replace(password, '[已隐藏]')
        raise AppError(f'{action}失败：{message[:240]}')

    def prepare(self):
        """Resolve the school's active protocol BEFORE disconnecting a session."""
        with self.context_lock:
            context = dict(self.context)
        if not context.get('ipv4'):
            self.status()
            context = dict(self.context)
        encode = lambda value: base64.b64encode(value.encode()).decode('ascii')
        response = self.jsonp(':802/eportal/portal/page/loadConfig', {
            'program_index': '', 'wlan_user_ip': encode(context.get('ipv4', '')),
            'wlan_user_ipv6': encode(context.get('ipv6', '')), 'wlan_vlan_id': context.get('vlan', '1'),
            'wlan_ac_ip': '', 'wlan_ap_mac': '000000000000', 'wlan_user_ssid': '',
            'wlan_user_areaid': '', 'gw_id': '000000000000'})
        config = response.get('data')
        if str(response.get('code')) != '1' or not isinstance(config, dict):
            raise AppError('无法读取学校认证配置，已停止操作；请稍后重试。')
        if str(config.get('login_method', '0')) not in ('0', '1', '9'):
            raise AppError('学校认证方式已变化，当前版本暂不支持，已保留当前连接。')
        if str(config.get('en_md5', '0')) != '0':
            raise AppError('学校启用了新的密码校验方式，当前版本暂不支持。')
        self.config = config

    def device_params(self):
        with self.context_lock:
            context = dict(self.context)
        if not context.get('ipv4'):
            raise AppError('无法识别本机校园网 IP，请刷新状态后重试。')
        return {'wlan_user_ip': context['ipv4'], 'wlan_user_ipv6': context.get('ipv6', ''),
                'wlan_user_mac': context.get('mac') or '000000000000',
                'wlan_ac_ip': '', 'wlan_ac_name': ''}

    def login(self, account, password):
        if self.config is None:
            self.prepare()
        method = str(self.config.get('login_method', '0'))
        if method in ('1', '9'):
            prefix = ',0,' if str(self.config.get('account_prefix', '0')) == '1' else ''
            params = dict(self.device_params(), login_method=method, user_account=prefix + account,
                          user_password=password, term_type='1', terminal_type='1',
                          term_ua='Mozilla/5.0 (Windows NT 10.0; Win64; x64)')
            response = self.jsonp(':802/eportal/portal/login', params)
        else:
            response = self.jsonp('/drcom/login', {
                'DDDDD': account, 'upass': password, 'R1': '0', 'R2': '', 'R3': '0', 'R6': '0',
                'para': '00', '0MKKey': '123456', 'v4ip': self.context['ipv4'],
                'v6ip': self.context.get('ipv6', ''), 'terminal_type': '1'})
        self.check_result(response, '登录', password)

    def online_devices(self):
        """Read-only list of the account's online terminals, as the official page polls it."""
        with self.context_lock:
            context = dict(self.context)
        if not context.get('ipv4'):
            self.status()
            with self.context_lock:
                context = dict(self.context)
        data = self.jsonp(':802/eportal/portal/online_list', {
            'user_account': '', 'user_password': '123',
            'wlan_user_mac': context.get('mac') or '000000000000',
            'wlan_user_ip': context['ipv4']})
        if str(data.get('result')) != '1':
            message = data.get('msg') or '门户未返回在线终端信息'
            raise AppError(f'查询在线终端失败：{str(message)[:120]}')
        devices = []
        for item in data.get('list') or []:
            if not isinstance(item, dict):
                continue
            mac = str(item.get('online_mac') or '').strip()
            if re.fullmatch(r'[0-9a-fA-F]{12}', mac):
                mac = ':'.join(mac[i:i + 2] for i in range(0, 12, 2)).upper()
            devices.append(dict(account=str(item.get('user_account') or '').strip(),
                                ipv4=str(item.get('online_ip') or '').strip(),
                                mac=mac, since=str(item.get('online_time') or '').strip(),
                                seconds=number(item.get('time_long')),
                                alias=str(item.get('device_alias') or '').strip(),
                                bound=str(item.get('is_bind_mac')) == '1',
                                perceive=str(item.get('is_perceive')) == '1',
                                owner=str(item.get('is_owner_ip')) == '1'))
        return devices

    def logout(self):
        if self.config is None:
            self.prepare()
        config = self.config
        params = self.device_params()
        if str(config.get('un_bind_mac')) == '1' and str(config.get('register_mode')) in ('1', '3', '4'):
            if params['wlan_user_mac'] == '000000000000' or not self.context.get('account'):
                raise AppError('无法识别当前账号或本机 MAC，已停止注销，请刷新状态后重试。')
            # Official wc() unbinds only this device, preventing immediate MAC reauthentication.
            response = self.jsonp(':802/eportal/portal/mac/unbind', dict(params, user_account=self.context['account']))
            self.check_result(response, '注销本机连接')
        elif str(config.get('login_method')) in ('1', '9') or str(config.get('ac_logout', '0')) != '0':
            response = self.jsonp(':802/eportal/portal/logout', dict(params,
                login_method=str(config.get('login_method', '0')), ac_logout=str(config.get('ac_logout', '0')),
                register_mode=str(config.get('register_mode', '1')), wlan_vlan_id=self.context.get('vlan', '1'),
                user_account='drcom', user_password='123'))
            self.check_result(response, '注销')
        else:
            self.check_result(self.jsonp('/drcom/logout'), '注销')


def protect(value, decrypt=False):
    if os.name != 'nt':
        raise AppError('当前版本使用 Windows 账户加密，请在 Windows 上保存或使用账号。')
    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(value) if decrypt else value.encode('utf-8')
    buf = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    out = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(out)):
        raise AppError('无法读取或保存密码，请用保存账号时的 Windows 用户运行。')
    try:
        result = ctypes.string_at(out.pbData, out.cbData)
        return result.decode('utf-8') if decrypt else base64.b64encode(result).decode('ascii')
    finally:
        kernel.LocalFree(out.pbData)


class Profiles:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.items = []
        if self.path.exists():
            try:
                self.items = json.loads(self.path.read_text(encoding='utf-8'))['profiles']
                if not isinstance(self.items, list) or any(not all(k in p for k in ('id', 'account', 'label', 'secret')) for p in self.items):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise AppError('账号文件损坏；为避免覆盖，请先检查 data/profiles.json。') from None

    def public(self):
        with self.lock:
            return [{k: p[k] for k in ('id', 'account', 'label')} for p in self.items]

    def get(self, profile_id):
        with self.lock:
            p = next((p for p in self.items if p['id'] == profile_id), None)
            if p is None:
                raise AppError('账号不存在，请刷新列表。')
            return dict(p)

    def write(self, items):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps({'profiles': items}, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)
        self.items = items

    def save(self, data):
        account = str(data.get('account', '')).strip()
        label = str(data.get('label', '')).strip() or account
        password = data.get('password', '')
        if not re.fullmatch(r'[A-Za-z0-9_.@,-]{1,100}', account):
            raise AppError('请输入完整校园网账号，可包含字母、数字及 . _ @ , -。')
        if len(label) > 50 or not isinstance(password, str) or len(password) > 256:
            raise AppError('备注最多 50 字，密码最多 256 字。')
        with self.lock:
            old = self.get(data['id']) if data.get('id') else None
            if any(p['account'] == account and (not old or p['id'] != old['id']) for p in self.items):
                raise AppError('此账号已保存，请编辑已有账号。')
            if not password and (not old or old['account'] != account):
                raise AppError('新增或更换账号时，请填写密码。')
            item = dict(id=old['id'] if old else uuid.uuid4().hex, account=account, label=label,
                        secret=protect(password) if password else old['secret'])
            self.write([p for p in self.items if p['id'] != item['id']] + [item])

    def delete(self, profile_id):
        with self.lock:
            self.get(profile_id)
            self.write([p for p in self.items if p['id'] != profile_id])

    def export_items(self):
        """Decrypt all profiles for portable export; caller must protect the result."""
        with self.lock:
            return [dict(account=p['account'], label=p['label'],
                         password=protect(p['secret'], decrypt=True)) for p in self.items]

    def import_items(self, items):
        """Merge imported profiles; accounts already saved locally are skipped."""
        added = skipped = 0
        with self.lock:
            for item in items:
                if any(p['account'] == item['account'] for p in self.items):
                    skipped += 1
                    continue
                self.save({'id': None, 'account': item['account'],
                           'label': item['label'], 'password': item['password']})
                added += 1
        return added, skipped


EXPORT_ITERATIONS = 200_000
EXPORT_MAX_ITERATIONS = 2_000_000
EXPORT_LIMIT = 1_000_000


def _derive_keys(password, salt, iterations):
    if not password:
        raise AppError('请设置导出密码。')
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations, dklen=64)
    return key[:32], key[32:]


def _keystream_xor(key, data):
    """HMAC-SHA256 counter-mode keystream; symmetric."""
    out = bytearray()
    counter = 0
    while len(out) < len(data):
        out.extend(hmac.new(key, b'CQU-Net-Switch-export-v1' + counter.to_bytes(8, 'big'),
                            hashlib.sha256).digest())
        counter += 1
    return bytes(a ^ b for a, b in zip(data, out))


def export_profiles_text(items, password):
    """Serialize profiles into a password-encrypted, tamper-evident JSON document."""
    salt = os.urandom(16)
    enc_key, mac_key = _derive_keys(password, salt, EXPORT_ITERATIONS)
    plain = json.dumps({'profiles': items}, ensure_ascii=False).encode('utf-8')
    cipher = _keystream_xor(enc_key, plain)
    tag = hmac.new(mac_key, salt + cipher, hashlib.sha256).digest()
    return json.dumps({
        'app': 'CQU-Net-Switch', 'kind': 'account-export', 'version': 1,
        'kdf': {'name': 'PBKDF2-HMAC-SHA256', 'iterations': EXPORT_ITERATIONS,
                'salt': base64.b64encode(salt).decode('ascii')},
        'payload': base64.b64encode(cipher).decode('ascii'),
        'mac': base64.b64encode(tag).decode('ascii')}, ensure_ascii=False, indent=2)


def parse_profiles_import(text, password):
    """Verify and decrypt an exported document; returns [{account, label, password}]."""
    if len(text) > EXPORT_LIMIT * 4:
        raise AppError('导入文件过大，不是有效的账号导出文件。')
    try:
        doc = json.loads(text)
        kdf = doc['kdf']
        if doc.get('app') != 'CQU-Net-Switch' or doc.get('kind') != 'account-export':
            raise ValueError()
        iterations = int(kdf['iterations'])
        if not 1 <= iterations <= EXPORT_MAX_ITERATIONS or kdf.get('name') != 'PBKDF2-HMAC-SHA256':
            raise ValueError()
        salt = base64.b64decode(kdf['salt'])
        cipher = base64.b64decode(doc['payload'])
        tag = base64.b64decode(doc['mac'])
        if not salt or not cipher or len(cipher) > EXPORT_LIMIT:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise AppError('导入文件无法识别，请选择本软件导出的账号文件。') from None
    enc_key, mac_key = _derive_keys(password, salt, iterations)
    expected = hmac.new(mac_key, salt + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise AppError('解密失败：导出密码不正确，或文件已被修改。')
    try:
        items = json.loads(_keystream_xor(enc_key, cipher).decode('utf-8'))['profiles']
        if not isinstance(items, list) or len(items) > 500:
            raise ValueError()
        result = []
        for item in items:
            account, label, password = str(item['account']), str(item.get('label', '')), str(item['password'])
            if not account or not password or len(password) > 256:
                raise ValueError()
            result.append(dict(account=account, label=label, password=password))
        return result
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise AppError('导入文件内容损坏，未做任何更改。') from None


class Dashboard:
    def __init__(self, profiles, portal=None, pause=time.sleep):
        self.profiles, self.portal, self.pause = profiles, portal or Portal(), pause
        self.lock = threading.RLock()
        self.operation = {'state': 'idle', 'message': ''}
        self.events = []

    def event(self, message, kind='info'):
        with self.lock:
            self.events.insert(0, dict(at=now(), message=message, kind=kind))
            self.events = self.events[:40]

    def snapshot(self):
        with self.lock:
            return dict(operation=dict(self.operation), events=list(self.events))

    def progress(self, message):
        with self.lock:
            self.operation['message'] = message

    def verify(self, predicate, failure):
        for _ in range(5):
            self.pause(1.5)
            try:
                status = self.portal.status()
            except AppError:
                continue
            if predicate(status):
                return status
        raise AppError(failure)

    def start(self, action, profile_id=None):
        with self.lock:
            if self.operation['state'] == 'running':
                raise AppError('正在处理上一个操作，请稍候。')
            # Read/decrypt credentials BEFORE disconnecting the current account.
            profile = self.profiles.get(profile_id) if action == 'switch' else None
            password = protect(profile['secret'], decrypt=True) if profile else None
            self.operation = dict(state='running', message='正在确认当前状态…', startedAt=now())
            threading.Thread(target=self.run, args=(action, profile, password), daemon=True).start()

    def run(self, action, profile=None, password=None):
        try:
            current = self.portal.status()
            if action == 'switch' and current['online'] and current['account'] == profile['account']:
                message = '该账号已经在线，无需切换。'
            else:
                self.progress('正在读取学校认证配置…')
                self.portal.prepare()
                if current['online']:
                    self.progress('正在注销当前账号…')
                    self.portal.logout()
                    self.verify(lambda s: not s['online'], '未能确认当前账号已注销，已停止切换。请刷新状态后重试。')
                if action == 'switch':
                    self.progress('正在登录目标账号…')
                    self.portal.login(profile['account'], password)
                    self.progress('正在核对实际在线账号…')
                    self.verify(lambda s: s['online'] and s['account'] == profile['account'],
                                '未能确认目标账号上线。请检查账号、密码或设备限制，再刷新状态；原账号可能已下线。')
                    message = '已连接 ' + profile['account']
                else:
                    message = '已确认当前设备下线。'
            with self.lock:
                self.operation = dict(state='success', message=message, finishedAt=now())
            self.event(message, 'success')
        except Exception as error:
            message = str(error) if isinstance(error, AppError) else '操作遇到异常，请刷新状态后重试。'
            with self.lock:
                self.operation = dict(state='error', message=message, finishedAt=now())
            self.event(message, 'error')



"""Local CQU portal dashboard. Python 3.10+, no third-party dependencies."""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import threading
import time
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler, ProxyHandler
import uuid
import webbrowser

ROOT = Path(__file__).resolve().parent
PORTAL = 'https://login.cqu.edu.cn'


from core import AppError, Dashboard, Profiles, Portal, normalize_status, parse_jsonp, protect


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, content_type='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def valid_host(self):
        return self.headers.get('Host') in self.server.allowed_hosts

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {'error': '不允许的访问地址。'})
        path = urlsplit(self.path).path
        app = self.server.app
        try:
            if path == '/api/bootstrap':
                return self.send(200, dict(token=self.server.token, profiles=app.profiles.public(), **app.snapshot()))
            if path.startswith('/api/'):
                if not secrets.compare_digest(self.headers.get('X-App-Token', ''), self.server.token):
                    return self.send(403, {'error': '会话已过期，请刷新页面。'})
                if path == '/api/status':
                    return self.send(200, app.portal.status())
                if path == '/api/state':
                    return self.send(200, dict(profiles=app.profiles.public(), **app.snapshot()))
            assets = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
            if path in assets:
                name, mime = assets[path]
                return self.send(200, (ROOT / 'static' / name).read_bytes(), mime + '; charset=utf-8')
            self.send(404, {'error': '未找到页面。'})
        except AppError as error:
            self.send(502, {'error': str(error)})
        except Exception:
            self.send(500, {'error': '本机服务出现异常，请检查文件权限后重试。'})

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = 0
        # Drain bounded request bodies before an early rejection so Windows does
        # not reset the connection and hide the HTTP error from the client.
        body = self.rfile.read(length) if 0 < length <= 8192 else b''
        if (not self.valid_host() or self.headers.get('Origin') not in self.server.origins
                or not secrets.compare_digest(self.headers.get('X-App-Token', ''), self.server.token)):
            return self.send(403, {'error': '请求来源无效，请从本机看板操作。'})
        try:
            if not 0 < length <= 8192:
                return self.send(400, {'error': '请求大小无效。'})
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError()
            app = self.server.app
            with app.lock:
                if app.operation['state'] == 'running':
                    raise AppError('账号操作正在进行，请稍后重试。')
                if self.path == '/api/profiles/save':
                    app.profiles.save(data)
                    app.event('已保存账号设置。')
                elif self.path == '/api/profiles/delete':
                    app.profiles.delete(data.get('id'))
                    app.event('已移除本机保存的账号。')
                elif self.path == '/api/switch':
                    app.start('switch', data.get('id'))
                elif self.path == '/api/logout':
                    app.start('logout')
                else:
                    return self.send(404, {'error': '操作不存在。'})
            self.send(200, dict(profiles=app.profiles.public(), **app.snapshot()))
        except (ValueError, TypeError):
            self.send(400, {'error': '请求格式错误。'})
        except AppError as error:
            self.send(409, {'error': str(error)})
        except Exception:
            self.send(500, {'error': '保存失败，请检查本机文件权限。'})


def make_server(port=8765, data_dir=None):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    actual_port = server.server_address[1]
    server.allowed_hosts = {f'127.0.0.1:{actual_port}', f'localhost:{actual_port}'}
    server.origins = {'http://' + host for host in server.allowed_hosts}
    server.token = secrets.token_urlsafe(32)
    server.app = Dashboard(Profiles(Path(data_dir or ROOT / 'data') / 'profiles.json'))
    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='重庆大学校园网本机看板')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    try:
        server = make_server(args.port)
        url = f'http://127.0.0.1:{server.server_address[1]}'
        print('CQU dashboard: ' + url, flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        server.serve_forever()
    except (OSError, AppError) as error:
        print('Unable to start: ' + str(error))
        raise SystemExit(1)
    except KeyboardInterrupt:
        server.server_close()

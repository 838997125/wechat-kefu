# -*- coding: utf-8 -*-
"""Web 管理面板：局域网可访问，带登录密码认证。"""
import hashlib
import logging
import os
import secrets
import time
from collections import deque

from flask import Flask, jsonify, request, send_from_directory

log = logging.getLogger('kefu')
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

# 登录会话 token -> 过期时间戳（内存，重启后需重新登录）
_SESSIONS = {}
_SESSION_TTL = 7 * 24 * 3600  # 7 天免登


def _pwd_hash(pwd):
    return hashlib.sha256(('kefu-panel:' + str(pwd)).encode('utf-8')).hexdigest()


class PanelServer:
    def __init__(self, cfg, storage, bot, log_path, port=43991):
        self.cfg = cfg
        self.storage = storage
        self.bot = bot
        self.log_path = log_path
        self.port = port
        self.app = Flask(__name__, static_folder=None)
        self._log_buf = deque(maxlen=2000)

        class BufferHandler(logging.Handler):
            def emit(this, record):
                self._log_buf.append(this.format(record))
        bh = BufferHandler()
        bh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S'))
        logging.getLogger('kefu').addHandler(bh)

        self._routes()

    def _panel_pwd(self):
        """面板登录密码（可在 config 的 general.panel_password 配置），默认 kefu2026。"""
        return str(self.cfg.general().get('panel_password', '') or 'kefu2026')

    def _authed(self, req):
        """校验请求是否已登录：Header token 或 query token，或本机回环（本机免登）。"""
        # 本机访问免登录（方便开机自动打开）
        if req.remote_addr in ('127.0.0.1', '::1', 'localhost'):
            return True
        token = req.headers.get('X-Panel-Token') or req.args.get('token') or ''
        exp = _SESSIONS.get(token)
        if exp and exp > time.time():
            _SESSIONS[token] = time.time() + _SESSION_TTL
            return True
        return False

    def _routes(self):
        app = self.app

        @app.before_request
        def _require_auth():
            # 登录接口、静态资源放行；其余 API 需要登录
            if request.path == '/api/login':
                return None
            if request.path.startswith('/api/'):
                if not self._authed(request):
                    return jsonify({'ok': False, 'auth_required': True, 'error': '未登录或登录已过期'}), 401
            return None

        @app.get('/')
        def index():
            return send_from_directory(STATIC_DIR, 'index.html')

        @app.get('/<path:path>')
        def static_files(path):
            return send_from_directory(STATIC_DIR, path)

        @app.post('/api/login')
        def api_login():
            data = request.get_json(force=True, silent=True) or {}
            pwd = data.get('password', '')
            if _pwd_hash(pwd) == _pwd_hash(self._panel_pwd()):
                token = secrets.token_hex(16)
                _SESSIONS[token] = time.time() + _SESSION_TTL
                # 清掉过期 session
                now = time.time()
                for k in [t for t, e in _SESSIONS.items() if e < now]:
                    _SESSIONS.pop(k, None)
                return jsonify({'ok': True, 'token': token,
                                'local': request.remote_addr in ('127.0.0.1', '::1', 'localhost')})
            return jsonify({'ok': False, 'error': '密码错误'}), 403

        @app.get('/api/authcheck')
        def authcheck():
            return jsonify({'ok': True, 'authed': self._authed(request),
                            'local': request.remote_addr in ('127.0.0.1', '::1', 'localhost')})

        @app.get('/api/status')
        def api_status():
            return jsonify(self.bot.status())

        @app.get('/api/config')
        def get_config():
            return jsonify(self.cfg.data)

        @app.put('/api/config')
        def put_config():
            data = request.get_json(force=True, silent=True)
            if not isinstance(data, dict):
                return jsonify({'ok': False, 'error': '配置格式错误'}), 400
            try:
                self.cfg.save(data)
                self.cfg.load(force=True)
                log.info('配置已保存并热加载')
                return jsonify({'ok': True})
            except Exception as e:
                log.exception('保存配置失败')
                return jsonify({'ok': False, 'error': str(e)}), 500

        @app.post('/api/bot/<action>')
        def bot_action(action):
            if action == 'pause':
                self.bot.paused = True
                return jsonify({'ok': True, 'paused': True})
            if action == 'resume':
                self.bot.paused = False
                return jsonify({'ok': True, 'paused': False})
            if action == 'reset_ai':
                ev, cmd = self.bot.post_command({'action': 'reset_ai'})
                ev.wait(timeout=10)
                return jsonify({'ok': True})
            return jsonify({'ok': False, 'error': 'unknown action'}), 400

        @app.post('/api/scan_sessions')
        def scan_sessions():
            ev, cmd = self.bot.post_command({'action': 'scan_sessions'})
            if not ev.wait(timeout=30):
                return jsonify({'ok': False, 'error': '超时（微信忙碌中）'}), 504
            if 'error' in cmd['_result']:
                return jsonify({'ok': False, 'error': cmd['_result']['error']}), 500
            monitored = {c['name'] for c in self.cfg.chats(enabled_only=False) if c.get('enabled')}
            for s in cmd['_result']['sessions']:
                s['monitored'] = s['name'] in monitored
            return jsonify({'ok': True, 'sessions': cmd['_result']['sessions']})

        @app.post('/api/send')
        def manual_send():
            data = request.get_json(force=True, silent=True) or {}
            chat, text = data.get('chat'), data.get('text', '')
            if not chat or not text:
                return jsonify({'ok': False, 'error': 'chat/text 必填'}), 400
            ev, cmd = self.bot.post_command({'action': 'send', 'chat': chat, 'text': text})
            if not ev.wait(timeout=60):
                return jsonify({'ok': False, 'error': '发送超时'}), 504
            return jsonify({'ok': bool(cmd['_result'].get('ok'))})

        @app.post('/api/ai/test')
        def ai_test():
            data = request.get_json(force=True, silent=True) or {}
            ai_cfg = data.get('ai') or self.cfg.ai()
            try:
                reply = self.bot.ai.test(ai_cfg)
                return jsonify({'ok': True, 'reply': reply})
            except Exception as e:
                return jsonify({'ok': False, 'error': str(e)}), 200

        @app.get('/api/messages')
        def messages():
            chat = request.args.get('chat') or None
            limit = min(int(request.args.get('limit', 200)), 1000)
            return jsonify({'ok': True, 'messages': self.storage.recent(chat, limit),
                            'chats': self.storage.chat_names()})

        @app.get('/api/logs')
        def logs():
            return jsonify({'ok': True, 'logs': list(self._log_buf)[-int(request.args.get('lines', 200)):]})

        @app.get('/api/routes/logs')
        def route_logs():
            shadow = request.args.get('shadow')
            shadow = None if shadow in (None, '', 'all') else (shadow == '1')
            return jsonify({'ok': True, 'logs': self.storage.route_recent(limit=300, shadow=shadow)})

    def run(self, host='0.0.0.0'):
        # 绑定 0.0.0.0 支持局域网客服访问（带登录密码认证）
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        self.app.run(host=host, port=self.port, threaded=True, use_reloader=False)

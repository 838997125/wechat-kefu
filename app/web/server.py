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

    def _is_local_request(self, req):
        """判断是否为本机直连访问（免登）。
        经 Cloudflare 隧道/反代的公网请求会带 CF-Connecting-IP / X-Forwarded-For 头，
        这类请求即使 remote_addr 是 127.0.0.1（隧道本地回环转发）也不算本机，必须登录。"""
        # 公网/反代标识头存在 -> 非本机直连
        if req.headers.get('CF-Connecting-IP') or req.headers.get('X-Forwarded-For'):
            return False
        return req.remote_addr in ('127.0.0.1', '::1', 'localhost')

    def _authed(self, req):
        """校验请求是否已登录：Header token 或 query token，或本机直连（本机免登）。"""
        # 本机直连免登录（方便开机自动打开）；经隧道的公网请求不免登
        if self._is_local_request(req):
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
                                'local': self._is_local_request(request)})
            return jsonify({'ok': False, 'error': '密码错误'}), 403

        @app.get('/api/authcheck')
        def authcheck():
            return jsonify({'ok': True, 'authed': self._authed(request),
                            'local': self._is_local_request(request)})

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

        # ---- 人工反馈：忽略某条转发日志（两档）----
        @app.post('/api/routes/log/<int:log_id>/ignore')
        def route_log_ignore(log_id):
            data = request.get_json(force=True, silent=True) or {}
            scope = data.get('scope', 'learn')  # once=仅此条  learn=此条+学习
            row = self.storage.route_log_get(log_id)
            if not row:
                return jsonify({'ok': False, 'error': '日志不存在'}), 404
            self.storage.set_log_ignored(log_id, True)
            from .. import intention as _it
            content = row.get('original') or row.get('forward') or ''
            source = row.get('source_chat') or ''
            rcfg = self.cfg.data.get('routing', {}) or {}
            cgroups = rcfg.get('customer_groups') or []
            role = 'zhongtong' if source == '药商通c端中通快递沟通群' else (
                'customer_src' if source in cgroups else 'other')
            norm = '[' + role + ']' + _it.normalize(content)
            if scope == 'once':
                self.storage.feedback_add('ignore_once', source, content, norm, log_id=log_id)
            else:
                # 学习忽略：精确签名压制（写入意图库 routes=[]）+ 作为负样本喂 LLM
                self.storage.feedback_add('ignore', source, content, norm, log_id=log_id)
                try:
                    fp = _it.fingerprint(content)
                    self.bot.intent_store.remember(norm, fp, row.get('sender', ''), [], content, source='manual')
                except Exception as e:
                    log.warning('写入忽略学习样本失败: %s', e)
            return jsonify({'ok': True, 'scope': scope})

        @app.post('/api/routes/log/<int:log_id>/unignore')
        def route_log_unignore(log_id):
            self.storage.set_log_ignored(log_id, False)
            self.storage.feedback_delete_by_log(log_id)
            return jsonify({'ok': True})

        # ---- 人工转发规则 ----
        @app.get('/api/manual_rules')
        def manual_rules_list():
            return jsonify({'ok': True, 'rules': self.storage.manual_rule_list()})

        @app.post('/api/manual_rules')
        def manual_rules_add():
            data = request.get_json(force=True, silent=True) or {}
            source = (data.get('source_chat') or '').strip()
            target = (data.get('target_chat') or '').strip()
            pattern = (data.get('pattern') or '').strip()
            if not source or not target or not pattern:
                return jsonify({'ok': False, 'error': '源群、目标群、转发内容均必填'}), 400
            if source == target:
                return jsonify({'ok': False, 'error': '源群和目标群不能相同'}), 400
            rid = self.storage.manual_rule_add(source, target, pattern, data.get('note', ''))
            # 作为正样本喂 LLM（帮助理解同类措辞）
            self.storage.feedback_add('forward', source, pattern, '', target_chat=target)
            return jsonify({'ok': True, 'id': rid})

        @app.post('/api/manual_rules/<int:rule_id>/toggle')
        def manual_rules_toggle(rule_id):
            data = request.get_json(force=True, silent=True) or {}
            self.storage.manual_rule_set_enabled(rule_id, bool(data.get('enabled', True)))
            return jsonify({'ok': True})

        @app.delete('/api/manual_rules/<int:rule_id>')
        def manual_rules_delete(rule_id):
            self.storage.manual_rule_delete(rule_id)
            return jsonify({'ok': True})

        # ---- 停止整个服务进程（托盘/面板用）----
        @app.post('/api/bot/shutdown')
        def bot_shutdown():
            import os, threading
            def _exit():
                time.sleep(0.6)
                try:
                    self.bot.stop()
                except Exception:
                    pass
                os._exit(0)
            threading.Thread(target=_exit, daemon=True).start()
            return jsonify({'ok': True})

    def run(self, host='0.0.0.0'):
        # 绑定 0.0.0.0 支持局域网客服访问（带登录密码认证）
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        self.app.run(host=host, port=self.port, threaded=True, use_reloader=False)

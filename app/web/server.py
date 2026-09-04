# -*- coding: utf-8 -*-
"""Web 管理面板：本地浏览器可视化配置规则/会话/AI，查看消息与日志。"""
import logging
import os
from collections import deque

from flask import Flask, jsonify, request, send_from_directory

log = logging.getLogger('kefu')
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')


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

    def _routes(self):
        app = self.app

        @app.get('/')
        def index():
            return send_from_directory(STATIC_DIR, 'index.html')

        @app.get('/<path:path>')
        def static_files(path):
            return send_from_directory(STATIC_DIR, path)

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

    def run(self, host='127.0.0.1'):
        # 关闭 Flask 默认启动日志噪音
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        self.app.run(host=host, port=self.port, threaded=True, use_reloader=False)

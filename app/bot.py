# -*- coding: utf-8 -*-
"""机器人主流程：监听 → 落库 → 新人欢迎 → 关键词规则 → AI 兜底 → 回复。"""
import logging
import queue
import random
import threading
import time
from collections import deque
from datetime import datetime

from . import rules as rule_engine
from .ai import AIClient
from .driver import WxDriver, parse_welcome_names

log = logging.getLogger('kefu')


class Bot:
    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.storage = storage
        self.driver = WxDriver(cfg.general())
        self.ai = AIClient()
        self._seen = {}
        self._last_reply = {}
        self._reply_times = {}
        self.paused = False
        self.started_at = None
        self._thread = None
        self._cmd_q = queue.Queue()
        self._stop = threading.Event()
        self.status_msg = '未启动'

    # ---------- 生命周期 ----------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='wxkefu-bot', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        self.started_at = datetime.now()
        try:
            self.status_msg = '正在连接微信…'
            self.driver.start()
            self.status_msg = '运行中'
        except Exception as e:
            self.status_msg = f'微信连接失败: {e}'
            log.error('微信连接失败: %s', e)
            return
        while not self._stop.is_set():
            try:
                self._drain_commands()
                self.cfg.load()  # 热加载
                if not self.paused and self.driver.ready:
                    for m in self.driver.poll(self.cfg.chats()):
                        self._on_message(m)
                else:
                    time.sleep(0.5)
            except Exception as e:
                log.error('轮询异常: %s', e)
                time.sleep(2)
            time.sleep(float(self.cfg.general().get('poll_interval_sec', 1.5)))
        self.status_msg = '已停止'

    # ---------- 面板命令队列（wxauto 对象有线程亲和性，必须在 bot 线程调用） ----------

    def post_command(self, cmd):
        """cmd: {'action': 'scan_sessions'|'send'|'reset_ai', ...}，返回 threading.Event 结果。"""
        ev = threading.Event()
        cmd['_ev'] = ev
        cmd['_result'] = {}
        self._cmd_q.put(cmd)
        return ev, cmd

    def _drain_commands(self):
        while True:
            try:
                cmd = self._cmd_q.get_nowait()
            except queue.Empty:
                return
            try:
                action = cmd['action']
                if action == 'scan_sessions':
                    cmd['_result']['sessions'] = self.driver.scan_sessions()
                elif action == 'send':
                    ok = self._do_send(cmd['chat'], cmd['text'], None)
                    cmd['_result']['ok'] = ok
                elif action == 'reset_ai':
                    self.ai.reset(cmd.get('session_key'))
                    cmd['_result']['ok'] = True
                else:
                    cmd['_result']['error'] = f'unknown action {action}'
            except Exception as e:
                cmd['_result']['error'] = str(e)
                log.exception('命令执行失败: %s', e)
            finally:
                cmd['_ev'].set()

    def status(self):
        g = self.cfg.general()
        s = {
            'status': self.status_msg,
            'paused': self.paused,
            'wechat_ready': self.driver.ready and self.driver.online(),
            'account': self.driver.account,
            'started_at': self.started_at.strftime('%Y-%m-%d %H:%M:%S') if self.started_at else None,
            'uptime_sec': int((datetime.now() - self.started_at).total_seconds()) if self.started_at else 0,
            'chats': [c['name'] for c in self.cfg.chats()],
            'rules_enabled': len(self.cfg.rules()),
            'ai_enabled': bool(self.cfg.ai().get('enabled')),
        }
        s.update(self.storage.stats_today())
        return s

    # ---------- 消息处理 ----------

    def _on_message(self, m):
        now = time.time()
        self._gc_seen(now)
        if m.fp in self._seen:
            return
        self._seen[m.fp] = now

        if m.attr == 'self' and not self.cfg.general().get('record_self', True):
            return
        if not self.storage.record(m.as_dict()):
            return

        if m.is_history:
            log.info('基线消息 [%s] %s(%s): %s', m.chat, m.sender or '-', m.mtype,
                     m.content[:80].replace('\n', ' '))
            return
        log.info('消息 [%s] %s(%s): %s', m.chat, m.sender or '-', m.mtype,
                 m.content[:80].replace('\n', ' '))

        # 新人入群欢迎
        if m.attr == 'system':
            self._handle_welcome(m)
            return
        if m.attr != 'friend':
            return

        g = self.cfg.general()
        bot_names = g.get('bot_names', [])
        mentioned = rule_engine.is_bot_mentioned(m.content, bot_names)
        clean = rule_engine.strip_bot_mentions(m.content, bot_names)

        if m.mtype not in ('text', 'quote'):
            log.info('非文本消息（%s），仅记录', m.mtype)
            return
        if not clean:
            return

        # 1) 关键词规则
        rule = rule_engine.match_rule(self.cfg.rules(), m.chat, m.chat_type, clean, mentioned)
        if rule:
            replies = rule_engine.render_replies(rule, m.sender)
            at_list = rule_engine.resolve_at(rule.get('at'), m.sender, m.chat_type)
            for i, text in enumerate(replies):
                self._reply(m, text, src=f"rule:{rule.get('name', '?')}",
                            at=at_list if i == 0 else None)
            self.storage.mark_replied(m.fp, f"rule:{rule.get('name', '?')}", ' / '.join(replies)[:500])
            return

        # 2) AI 兜底
        ai_cfg = self.cfg.ai()
        if not ai_cfg.get('enabled') or not ai_cfg.get('api_key'):
            if m.chat_type == 'group' and not mentioned:
                return
            return
        trigger = ai_cfg.get('trigger', 'at')
        if m.chat_type == 'group' and trigger == 'at' and not mentioned:
            return
        self._ai_reply(m, clean, ai_cfg)

    def _handle_welcome(self, m):
        if m.chat_type != 'group':
            return
        wc = self.cfg.welcome()
        if not wc.get('enabled'):
            return
        chats = wc.get('chats', []) or []
        if chats and m.chat not in chats:
            return
        names = parse_welcome_names(m.content)
        if not names:
            return
        at_list = names if wc.get('at_newcomer', True) else None
        text = wc.get('message', '欢迎新成员~').replace('{name}', '、'.join(names))
        ok = self._reply(m, text, src='welcome', at=at_list)
        if ok:
            self.storage.mark_replied(m.fp, 'welcome', text[:500])
        log.info('欢迎语 [%s] 新成员: %s', m.chat, names)

    def _ai_reply(self, m, clean_text, ai_cfg):
        session_key = f"group:{m.chat}" if m.chat_type == 'group' else f"dm:{m.sender}"
        try:
            reply = self.ai.chat(ai_cfg, session_key, m.chat, m.sender, clean_text)
        except Exception as e:
            log.error('AI 调用失败: %s', e)
            return
        if reply:
            at = [m.sender] if (m.chat_type == 'group' and self.cfg.general().get('at_sender', True)) else None
            self._reply(m, reply, src='ai', at=at)
            self.storage.mark_replied(m.fp, 'ai', reply[:500])

    # ---------- 回复（延时 / 频控 / 拆条） ----------

    def _reply(self, m, text, src, at=None):
        if not text or not text.strip():
            return False
        g = self.cfg.general()
        now = time.time()
        dmin = float(g.get('reply_delay_min', 1.0))
        dmax = float(g.get('reply_delay_max', 3.0))
        if dmax > 0:
            time.sleep(random.uniform(min(dmin, dmax), max(dmin, dmax)))
        dq = self._reply_times.setdefault(m.chat, deque())
        while dq and time.time() - dq[0] > 60:
            dq.popleft()
        limit = int(g.get('max_replies_per_minute', 12))
        if len(dq) >= limit:
            log.warning('[%s] 超过每分钟 %d 条上限，跳过', m.chat, limit)
            return False
        chunks = rule_engine.split_text(text, int(g.get('split_length', 1500)))
        ok_all = True
        for i, chunk in enumerate(chunks):
            ok = self._do_send(m.chat, chunk, at if i == 0 else None)
            ok_all = ok_all and ok
            if i < len(chunks) - 1:
                time.sleep(random.uniform(0.8, 1.6))
        if ok_all:
            dq.append(time.time())
            self._last_reply[m.chat] = time.time()
            log.info('回复 [%s] (%s, %d段): %s', m.chat, src, len(chunks),
                     text[:80].replace('\n', ' '))
        return ok_all

    def _do_send(self, chat, text, at):
        return self.driver.send(chat, text, at=at)

    def _gc_seen(self, now, ttl=120):
        for k in [k for k, t in self._seen.items() if now - t > ttl]:
            self._seen.pop(k, None)

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
from . import router as route_engine
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
                if not self.driver.ready:
                    # 微信连接丢失：自动拉起重连
                    self.driver.heal_if_needed()
                    time.sleep(2)
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
        # is_new = 是否首次入库（真正的新消息）；is_history 是基线/旧消息
        is_new = self.storage.record(m.as_dict())

        if m.is_history or not is_new:
            log.info('基线消息 [%s] %s(%s): %s', m.chat, m.sender or '-', m.mtype,
                     m.content[:80].replace('\n', ' '))
            return
        log.info('消息 [%s] %s(%s): %s', m.chat, m.sender or '-', m.mtype,
                 m.content[:80].replace('\n', ' '))

        # 路由转发：对所有真实新消息生效（system 除外），独立于本群关键词回复
        if m.attr == 'friend':
            self._route_message(m)

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
            log.info('非文本消息（%s），仅记录+路由', m.mtype)
            return
        if not clean:
            return

        # 1) 关键词规则

        # 1) 关键词规则
        rule = rule_engine.match_rule(self.cfg.rules(), m.chat, m.chat_type, clean, mentioned)
        if rule:
            replies = rule_engine.choose_replies(rule, m.sender)
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

    def _route_message(self, m):
        """多群消息路由：匹配路由规则 → 影子模式只记录 / 正式模式转发到目标群。"""
        rcfg = self.cfg.data.get('routing', {}) or {}
        routes = rcfg.get('routes', []) or []
        if not routes:
            return
        shadow = bool(rcfg.get('shadow_mode', True))
        tracking = route_engine.extract_tracking_no(m.content)
        all_numbers = route_engine.extract_all_tracking(m.content)
        own_staff = rcfg.get('own_staff', ['<公司简称>'])
        # 己方客服发的消息默认不参与路由（避免把自己搬运的消息再转一遍），
        # 除非该路由显式 require 己方发送者（如 B群只处理<回写机器人名>，不在这里判断）
        if route_engine.is_own_staff(m.sender, own_staff):
            # <回写机器人名>等外部机器人不属己方；己方客服在 B 群的搬运、A群的发言都不触发转发
            if not m.sender or '<回写机器人名>' not in m.sender:
                return
        route = route_engine.match_route(routes, m.chat, m.sender, m.content, m.mtype)
        if not route:
            return
        # 去重 key：按单号去重的路由（催发货）用单号集合，否则用原文
        dedup_key = route_engine.route_dedup_key(route, m.content, all_numbers=all_numbers)
        rid = route.get('id', route.get('name', ''))
        # 防重复：同一去重key 正式发送过不再处理；同一模式下不重复记录
        if not shadow and self.storage.route_forwarded(rid, dedup_key):
            return
        if self.storage.route_log_exists(rid, dedup_key, shadow):
            return
        # 若需要@发单人：按单号在目标群（通常是A群）回溯发该单号的客服昵称
        requester = ''
        if route.get('at_requester') and tracking:
            requester = self.storage.find_sender_by_tracking(
                route.get('target'), tracking, own_staff=rcfg.get('own_staff'))
        forward_text = route_engine.build_forward(
            route, m.content, m.sender, tracking, requester=requester, all_numbers=all_numbers)
        if forward_text is None:
            # 该路由暂不满足转发条件（如催发货但消息里没有快递单号），跳过不记日志
            return
        target = route.get('target')
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if shadow:
            self.storage.route_log({
                'ts': ts, 'route_name': rid, 'source_chat': m.chat,
                'target_chat': target, 'sender': m.sender, 'tracking_no': tracking,
                'mode': route.get('mode'), 'shadow': True,
                'original': dedup_key, 'forward': forward_text, 'status': 'shadow'})
            log.info('【影子】[%s] %s -> %s（单号:%s @%s）转发预览: %s',
                     m.chat, route.get('name'), target, tracking or '无', requester or '未匹配',
                     forward_text[:60].replace('\n', ' '))
            return
        # 正式转发（带频控/延时；@发单人 由发送通道处理）
        ok = self._do_send_route(target, forward_text, at_requester=requester if route.get('at_requester') else None)
        status = 'sent' if ok else 'failed'
        self.storage.route_log({
            'ts': ts, 'route_name': rid, 'source_chat': m.chat,
            'target_chat': target, 'sender': m.sender, 'tracking_no': tracking,
            'mode': route.get('mode'), 'shadow': False,
            'original': dedup_key, 'forward': forward_text, 'status': status})
        log.info('【转发】%s -> %s：%s（%s）', m.chat, target, forward_text[:60].replace('\n', ' '), status)

    def _do_send_route(self, chat, text, at_requester=None):
        """路由转发：延时+频控后发文本到目标群。at_requester: 需@的发单人昵称。"""
        g = self.cfg.general()
        now = time.time()
        dmin = float(g.get('reply_delay_min', 3))
        dmax = float(g.get('reply_delay_max', 8))
        if dmax > 0:
            time.sleep(random.uniform(min(dmin, dmax), max(dmin, dmax)))
        dq = self._reply_times.setdefault(chat, deque())
        while dq and time.time() - dq[0] > 60:
            dq.popleft()
        limit = int(g.get('max_replies_per_minute', 8))
        if len(dq) >= limit:
            log.warning('[路由] %s 超过每分钟 %d 条上限，跳过', chat, limit)
            return False
        chunks = rule_engine.split_text(text, int(g.get('split_length', 1500)))
        at_list = [at_requester] if at_requester else None
        ok_all = True
        for i, chunk in enumerate(chunks):
            # 只在第一段 @（wxauto at 参数会在文本前插入 @）
            ok = self.driver.send(chat, chunk, at=(at_list if i == 0 else None))
            ok_all = ok_all and ok
            if i < len(chunks) - 1:
                time.sleep(random.uniform(0.8, 1.6))
        if ok_all:
            dq.append(time.time())
        return ok_all

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

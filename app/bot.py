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
from . import intention
from .ai import AIClient
from .driver import WxDriver, parse_welcome_names

log = logging.getLogger('kefu')


class Bot:
    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.storage = storage
        self.driver = WxDriver(cfg.general())
        self.ai = AIClient()
        self.intent_store = intention.IntentStore(storage)
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
                # 心跳探测：微信重启后旧 wxauto 句柄可能失效，主动检测并自愈重连
                self.driver.heartbeat()
                if not self.paused and self.driver.ready:
                    for m in self.driver.poll(self.cfg.chats()):
                        self._on_message(m)
                else:
                    time.sleep(2)
            except Exception as e:
                log.error('轮询异常: %s', e)
                # 异常时也尝试自愈
                try:
                    self.driver.ready = False
                    self.driver.heal_if_needed(force=True)
                except Exception:
                    pass
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
            'llm_intent': bool((self.cfg.data.get('routing', {}) or {}).get('llm_intent', {}).get('enabled')),
            'intent_stats': self.intent_store.stats(),
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
        """多群消息路由（大模型意图识别 + 关键词降级）。
        LLM 判定目标群字母 [B/C/D/A]，映射到具体群；模型失败/关闭时降级关键词路由。"""
        rcfg = self.cfg.data.get('routing', {}) or {}
        routes = rcfg.get('routes', []) or []
        if not routes:
            return
        shadow = bool(rcfg.get('shadow_mode', True))
        tracking = route_engine.extract_tracking_no(m.content)
        all_numbers = route_engine.extract_all_tracking(m.content)
        own_staff = rcfg.get('own_staff', ['<公司简称>'])
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        targets = None  # 目标群字母列表
        llm_src = None
        llm_cfg = rcfg.get('llm_intent', {}) or {}
        if llm_cfg.get('enabled'):
            is_robot = '<回写机器人名>' in (m.sender or '')
            res, llm_src = intention.classify(
                m.sender, m.content, own_staff, llm_cfg, self.intent_store, is_robot=is_robot)
            if res is not None:
                targets = [t for t in res if t in ('B', 'C', 'D', 'A')]

        # 降级或 LLM 关闭：用关键词路由推断目标群
        if targets is None:
            targets = self._keyword_targets(routes, m, own_staff)
            llm_src = llm_src or 'keyword'

        if not targets:
            return

        # 目标群字母 -> 实际路由配置（必须校验路由的源群与消息所在群一致，避免跨群误映射）
        by_target = {}
        for rt in routes:
            tgt = rt.get('target', '')
            src = rt.get('source', '')
            # 源群必须匹配消息所在群，否则该路由对本条消息不适用
            if src != m.chat:
                continue
            if '药商通c端中通快递沟通群' in tgt:
                by_target.setdefault('B', rt)
            elif '<物流方>&<公司简称>' in tgt:
                by_target.setdefault('C', rt)
            elif 'C端审单发货售后' in tgt:
                by_target.setdefault('D', rt)
            elif src == '药商通c端中通快递沟通群':
                by_target.setdefault('A', rt)

        # B群<回写机器人名>失败结果只允许回A；B群其他消息一律不路由
        if m.chat == '药商通c端中通快递沟通群':
            is_robot = '<回写机器人名>' in (m.sender or '')
            if not is_robot:
                return
            targets = [t for t in targets if t == 'A']
        # A群发出的消息只能转 B/C/D（不能回A自己）
        if m.chat == '<品牌A>客服对接群':
            targets = [t for t in targets if t in ('B', 'C', 'D')]

        for letter in targets:
            rt = by_target.get(letter)
            if rt:
                self._process_one_route(rt, m, shadow, tracking, all_numbers, rcfg, own_staff, ts,
                                        llm_src=llm_src)

    def _keyword_targets(self, routes, m, own_staff):
        """降级：关键词路由 -> 目标群字母列表。"""
        matched = route_engine.match_routes(routes, m.chat, m.sender, m.content, m.mtype, own_staff=own_staff)
        letters = []
        for rt in matched:
            tgt = rt.get('target', '')
            if '药商通c端中通快递沟通群' in tgt:
                letters.append('B')
            elif '<物流方>&<公司简称>' in tgt:
                letters.append('C')
            elif 'C端审单发货售后' in tgt:
                letters.append('D')
            elif rt.get('source') == '药商通c端中通快递沟通群':
                letters.append('A')
        return letters

    def _process_one_route(self, route, m, shadow, tracking, all_numbers, rcfg, own_staff, ts, llm_src=''):
        """处理单条命中路由：去重、生成内容、影子记录或正式转发。"""
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
        if shadow:
            self.storage.route_log({
                'ts': ts, 'route_name': rid, 'source_chat': m.chat,
                'target_chat': target, 'sender': m.sender, 'tracking_no': tracking,
                'mode': route.get('mode'), 'shadow': True,
                'original': dedup_key, 'forward': forward_text, 'status': 'shadow'})
            log.info('【影子·%s】[%s] %s -> %s（单号:%s @%s）转发预览: %s',
                     llm_src or '?', m.chat, route.get('name'), target, tracking or '无',
                     requester or '未匹配', forward_text[:60].replace('\n', ' '))
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

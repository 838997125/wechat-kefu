# -*- coding: utf-8 -*-
"""SQLite 消息存储。"""
import os
import sqlite3
import threading


class Storage:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                chat        TEXT NOT NULL,
                chat_type   TEXT,
                sender      TEXT,
                attr        TEXT,
                mtype       TEXT,
                content     TEXT,
                fp          TEXT UNIQUE,
                replied     INTEGER DEFAULT 0,
                reply_src   TEXT,
                reply_text  TEXT
            )
        ''')
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS route_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                route_name  TEXT,
                source_chat TEXT,
                target_chat TEXT,
                sender      TEXT,
                tracking_no TEXT,
                mode        TEXT,
                shadow      INTEGER DEFAULT 1,
                original    TEXT,
                forward     TEXT,
                status      TEXT DEFAULT 'shadow',
                ignored     INTEGER DEFAULT 0
            )
        ''')
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS manual_rules (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                enabled     INTEGER DEFAULT 1,
                source_chat TEXT NOT NULL,
                target_chat TEXT NOT NULL,
                pattern     TEXT NOT NULL,
                note        TEXT,
                hits        INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        ''')
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS manual_feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                log_id      INTEGER,
                source_chat TEXT,
                content     TEXT,
                norm        TEXT,
                kind        TEXT,
                target_chat TEXT,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        ''')
        # 兼容旧库：route_logs 补 ignored 列
        cols = {r[1] for r in self.conn.execute('PRAGMA table_info(route_logs)').fetchall()}
        if 'ignored' not in cols:
            self.conn.execute('ALTER TABLE route_logs ADD COLUMN ignored INTEGER DEFAULT 0')
        self.conn.commit()
        self._lock = threading.Lock()

    def record(self, m):
        """插入一条消息（m 为 dict）；fp 重复返回 False。"""
        with self._lock:
            cur = self.conn.execute(
                'INSERT OR IGNORE INTO messages '
                '(ts, chat, chat_type, sender, attr, mtype, content, fp) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (m['ts'], m['chat'], m.get('chat_type', ''), m.get('sender', ''),
                 m['attr'], m['mtype'], m['content'], m['fp'])
            )
            self.conn.commit()
            return cur.rowcount > 0

    def mark_replied(self, fp, src, reply_text):
        with self._lock:
            self.conn.execute(
                'UPDATE messages SET replied=1, reply_src=?, reply_text=? WHERE fp=?',
                (src, reply_text, fp)
            )
            self.conn.commit()

    def recent(self, chat=None, limit=200):
        with self._lock:
            if chat:
                rows = self.conn.execute(
                    'SELECT ts,chat,sender,attr,mtype,content,replied,reply_src,reply_text '
                    'FROM messages WHERE chat=? ORDER BY id DESC LIMIT ?', (chat, limit)
                ).fetchall()
            else:
                rows = self.conn.execute(
                    'SELECT ts,chat,sender,attr,mtype,content,replied,reply_src,reply_text '
                    'FROM messages ORDER BY id DESC LIMIT ?', (limit,)
                ).fetchall()
        cols = ['ts', 'chat', 'sender', 'attr', 'mtype', 'content', 'replied', 'reply_src', 'reply_text']
        return [dict(zip(cols, r)) for r in rows]

    def chat_names(self):
        with self._lock:
            rows = self.conn.execute(
                'SELECT chat, COUNT(*) c, MAX(ts) last_ts FROM messages GROUP BY chat ORDER BY last_ts DESC'
            ).fetchall()
        return [{'name': r[0], 'count': r[1], 'last_ts': r[2]} for r in rows]

    def stats_today(self):
        with self._lock:
            total = self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE date(ts)=date('now','localtime')").fetchone()[0]
            replies = self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE replied=1 AND date(ts)=date('now','localtime')").fetchone()[0]
            last_ts = self.conn.execute(
                "SELECT ts FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        return {'messages_today': total, 'replies_today': replies,
                'last_message_ts': last_ts[0] if last_ts else None}

    def find_sender_by_tracking(self, chat, tracking_no, own_staff=None):
        """在指定群历史消息中，找包含该单号、且非己方客服/非机器人的最近发送人昵称。
        own_staff: 己方客服昵称关键词列表（这些人发的单号不算“客户发单人”）。
        """
        if not tracking_no:
            return ''
        own = own_staff or ['<公司简称>', '<己方客服D>', '<己方客服E>', '<己方客服B>', '<己方客服C>', '。。']

        def _is_own(s):
            return any(k in (s or '') for k in own) or '<回写机器人名>' in (s or '')

        with self._lock:
            rows = self.conn.execute(
                "SELECT sender, content FROM messages WHERE chat=? AND attr='friend' ORDER BY id DESC LIMIT 200",
                (chat,)
            ).fetchall()
        for sender, content in rows:
            if tracking_no in (content or '') and sender and not _is_own(sender):
                return sender
        for sender, content in rows:
            if tracking_no in (content or '') and sender and not _is_own(sender):
                return sender
        return ''

    # ---- 路由日志 ----
    def route_log(self, entry):
        with self._lock:
            self.conn.execute(
                'INSERT INTO route_logs '
                '(ts, route_name, source_chat, target_chat, sender, tracking_no, mode, shadow, original, forward, status) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (entry['ts'], entry.get('route_name'), entry.get('source_chat'), entry.get('target_chat'),
                 entry.get('sender'), entry.get('tracking_no', ''), entry.get('mode'),
                 1 if entry.get('shadow') else 0, entry.get('original', ''),
                 entry.get('forward', ''), entry.get('status', 'shadow'))
            )
            self.conn.commit()

    def route_log_exists(self, route_id, original, shadow):
        """防重复：同一路由+同一原文在同一模式下是否已记录过。"""
        with self._lock:
            row = self.conn.execute(
                'SELECT 1 FROM route_logs WHERE route_name=? AND original=? AND shadow=? LIMIT 1',
                (route_id, original, 1 if shadow else 0)
            ).fetchone()
        return row is not None

    def route_forwarded(self, route_id, original):
        """该消息是否已正式（非影子）转发过。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM route_logs WHERE route_name=? AND original=? AND shadow=0 AND status='sent' LIMIT 1",
                (route_id, original)
            ).fetchone()
        return row is not None

    def route_recent(self, limit=200, shadow=None):
        with self._lock:
            if shadow is None:
                rows = self.conn.execute(
                    'SELECT id,ts,route_name,source_chat,target_chat,sender,tracking_no,mode,shadow,original,forward,status,ignored '
                    'FROM route_logs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
            else:
                rows = self.conn.execute(
                    'SELECT id,ts,route_name,source_chat,target_chat,sender,tracking_no,mode,shadow,original,forward,status,ignored '
                    'FROM route_logs WHERE shadow=? ORDER BY id DESC LIMIT ?',
                    (1 if shadow else 0, limit)).fetchall()
        cols = ['id', 'ts', 'route_name', 'source_chat', 'target_chat', 'sender', 'tracking_no',
                'mode', 'shadow', 'original', 'forward', 'status', 'ignored']
        return [dict(zip(cols, r)) for r in rows]

    def route_log_get(self, log_id):
        with self._lock:
            row = self.conn.execute(
                'SELECT id,source_chat,target_chat,sender,original,forward,ignored '
                'FROM route_logs WHERE id=?', (log_id,)).fetchone()
        if not row:
            return None
        return dict(zip(['id', 'source_chat', 'target_chat', 'sender', 'original', 'forward', 'ignored'], row))

    def set_log_ignored(self, log_id, ignored):
        with self._lock:
            cur = self.conn.execute('UPDATE route_logs SET ignored=? WHERE id=?',
                                   (1 if ignored else 0, log_id))
            self.conn.commit()
            return cur.rowcount > 0

    # ---- 人工转发规则 ----
    def manual_rule_list(self, enabled_only=False):
        q = 'SELECT id,enabled,source_chat,target_chat,pattern,note,hits,created_at FROM manual_rules'
        if enabled_only:
            q += ' WHERE enabled=1'
        q += ' ORDER BY id DESC'
        with self._lock:
            rows = self.conn.execute(q).fetchall()
        cols = ['id', 'enabled', 'source_chat', 'target_chat', 'pattern', 'note', 'hits', 'created_at']
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            d['enabled'] = bool(d['enabled'])
            out.append(d)
        return out

    def manual_rule_add(self, source_chat, target_chat, pattern, note=''):
        with self._lock:
            cur = self.conn.execute(
                'INSERT INTO manual_rules (enabled,source_chat,target_chat,pattern,note) VALUES (1,?,?,?,?)',
                (source_chat, target_chat, pattern, note))
            self.conn.commit()
            return cur.lastrowid

    def manual_rule_set_enabled(self, rule_id, enabled):
        with self._lock:
            self.conn.execute('UPDATE manual_rules SET enabled=? WHERE id=?',
                              (1 if enabled else 0, rule_id))
            self.conn.commit()

    def manual_rule_delete(self, rule_id):
        with self._lock:
            cur = self.conn.execute('DELETE FROM manual_rules WHERE id=?', (rule_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def manual_rule_hit(self, rule_id):
        with self._lock:
            self.conn.execute('UPDATE manual_rules SET hits=hits+1 WHERE id=?', (rule_id,))
            self.conn.commit()

    # ---- 人工标注样本（喂 AI 学习） ----
    def feedback_add(self, kind, source_chat, content, norm='', target_chat='', log_id=None):
        with self._lock:
            cur = self.conn.execute(
                'INSERT INTO manual_feedback (log_id,source_chat,content,norm,kind,target_chat) VALUES (?,?,?,?,?,?)',
                (log_id, source_chat, content, norm, kind, target_chat))
            self.conn.commit()
            return cur.lastrowid

    def feedback_delete_by_log(self, log_id):
        with self._lock:
            self.conn.execute('DELETE FROM manual_feedback WHERE log_id=?', (log_id,))
            self.conn.commit()

    def ignore_norm_set(self):
        """返回需精确压制的归一化签名集合（仅忽略此条 + 学习忽略），命中即不转发。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT norm FROM manual_feedback WHERE kind IN ('ignore','ignore_once') AND norm<>''"
            ).fetchall()
        return {r[0] for r in rows}

    def feedback_examples(self, limit_per_kind=15):
        """返回正/负样本（各最近 limit_per_kind 条），用于拼 LLM prompt。"""
        with self._lock:
            neg = self.conn.execute(
                "SELECT content,source_chat FROM manual_feedback WHERE kind='ignore' "
                "ORDER BY id DESC LIMIT ?", (limit_per_kind,)).fetchall()
            pos = self.conn.execute(
                "SELECT content,source_chat,target_chat FROM manual_feedback WHERE kind='forward' "
                "ORDER BY id DESC LIMIT ?", (limit_per_kind,)).fetchall()
        return {
            'negative': [{'content': r[0], 'source_chat': r[1]} for r in reversed(neg)],
            'positive': [{'content': r[0], 'source_chat': r[1], 'target_chat': r[2]} for r in reversed(pos)],
        }

    def close(self):
        with self._lock:
            self.conn.close()

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
                status      TEXT DEFAULT 'shadow'
            )
        ''')
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
                    'SELECT ts,route_name,source_chat,target_chat,sender,tracking_no,mode,shadow,original,forward,status '
                    'FROM route_logs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
            else:
                rows = self.conn.execute(
                    'SELECT ts,route_name,source_chat,target_chat,sender,tracking_no,mode,shadow,original,forward,status '
                    'FROM route_logs WHERE shadow=? ORDER BY id DESC LIMIT ?',
                    (1 if shadow else 0, limit)).fetchall()
        cols = ['ts', 'route_name', 'source_chat', 'target_chat', 'sender', 'tracking_no',
                'mode', 'shadow', 'original', 'forward', 'status']
        return [dict(zip(cols, r)) for r in rows]

    def close(self):
        with self._lock:
            self.conn.close()

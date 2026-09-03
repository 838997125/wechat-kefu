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
        return {'messages_today': total, 'replies_today': replies}

    def close(self):
        with self._lock:
            self.conn.close()

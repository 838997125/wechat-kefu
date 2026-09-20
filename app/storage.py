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

    def recent(self, chat=None, limit=200, days=None):
        """最近消息；days 给定时只返回最近 N 天（面板默认近15天），但仍受 limit 约束。"""
        with self._lock:
            where, params = '', []
            if days:
                where = " WHERE ts >= datetime('now','localtime',?) "
                params.append('-%d days' % int(days))
            if chat:
                where += (' AND' if where else ' WHERE') + ' chat=?'
                params.append(chat)
            params.append(limit)
            rows = self.conn.execute(
                'SELECT ts,chat,sender,attr,mtype,content,replied,reply_src,reply_text '
                'FROM messages%s ORDER BY id DESC LIMIT ?' % where, params
            ).fetchall()
        cols = ['ts', 'chat', 'sender', 'attr', 'mtype', 'content', 'replied', 'reply_src', 'reply_text']
        return [dict(zip(cols, r)) for r in rows]

    def purge_old(self, keep_days):
        """删除超过保留期的消息与转发日志，回收空间。返回删除条数。0/负数表示不清理。"""
        try:
            keep_days = int(keep_days)
        except Exception:
            return 0
        if keep_days <= 0:
            return 0
        cutoff_expr = "datetime('now','localtime','-%d days')" % keep_days
        deleted = 0
        with self._lock:
            cur = self.conn.execute('DELETE FROM messages WHERE ts < ' + cutoff_expr)
            deleted += cur.rowcount
            cur = self.conn.execute('DELETE FROM route_logs WHERE ts < ' + cutoff_expr)
            deleted += cur.rowcount
            # 孤立的人工反馈（其日志已删）不影响压制，保留以学习；回收空间
            self.conn.commit()
            try:
                self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                if self.conn.execute('PRAGMA auto_vacuum').fetchone()[0] == 0:
                    self.conn.execute('VACUUM')
            except Exception:
                pass
        return deleted

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

    def tracking_list(self, days=0, route_name=None, status=None, dedup=True, limit=5000):
        """汇总转发日志里的快递单号（供客服快速复制/导出）。
        days=0 表示今天；route_name 过滤路由(如拦截转中通 rt_a2b)；status 过滤 sent/failed；
        dedup=True 同一只取最早一条。返回按时间正序的 dict 列表(含时间/单号/发单人/源群/目标群/状态)。"""
        where = []
        params = []
        if days and int(days) > 0:
            where.append("ts >= datetime('now','localtime',?)")
            params.append('-%d days' % int(days))
        else:
            where.append("date(ts)=date('now','localtime')")
        where.append("IFNULL(tracking_no,'')<>''")
        if route_name:
            where.append('route_name=?')
            params.append(route_name)
        if status:
            where.append('status=?')
            params.append(status)
        sqlw = ' WHERE ' + ' AND '.join(where)
        with self._lock:
            if dedup:
                rows = self.conn.execute(
                    'SELECT ts,tracking_no,sender,source_chat,target_chat,route_name,status '
                    'FROM route_logs ' + sqlw + ' GROUP BY tracking_no ORDER BY id ASC LIMIT ?',
                    params + [limit]).fetchall()
            else:
                rows = self.conn.execute(
                    'SELECT ts,tracking_no,sender,source_chat,target_chat,route_name,status '
                    'FROM route_logs ' + sqlw + ' ORDER BY id ASC LIMIT ?',
                    params + [limit]).fetchall()
        cols = ['ts', 'tracking_no', 'sender', 'source_chat', 'target_chat', 'route_name', 'status']
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

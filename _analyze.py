# -*- coding: utf-8 -*-
import sys, sqlite3
sys.stdout.reconfigure(encoding='utf-8')
conn = sqlite3.connect(r'D:\DeepSeekHarness\wechat-kefu\data\messages.db')
conn.row_factory = sqlite3.Row

print('===== 9/8 今早各群消息（按群）=====')
for chat in ['<品牌A>客服对接群','药商通c端中通快递沟通群','<物流方>&<公司简称>','C端审单发货售后']:
    rows = conn.execute("SELECT ts,sender,mtype,content FROM messages WHERE chat=? AND ts>='2026-09-08' ORDER BY id ASC", (chat,)).fetchall()
    print(f'\n--- {chat} ({len(rows)}条) ---')
    for r in rows:
        c = (r['content'] or '').replace('\n',' ')[:52]
        print('  ', r['ts'][11:], '['+r['mtype']+']', r['sender'][:12], c)

print('\n===== 9/8 今天产生的路由日志 =====')
for r in conn.execute("SELECT ts,source_chat,target_chat,sender,forward FROM route_logs WHERE ts>='2026-09-08' ORDER BY id ASC"):
    print('  ', r['ts'][11:], (r['source_chat'] or '?')[:8], '->', (r['target_chat'] or '?')[:8], '|', (r['forward'] or '')[:38])

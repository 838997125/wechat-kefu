# -*- coding: utf-8 -*-
"""
意图识别：关键词预筛 + 本地学习库 + 大模型判定（混合方案，可自进化）。

工作流程（对每条待判定消息）：
  1. rule_fast_path：己方水消息/图片/系统/空消息 -> NONE，不调模型
  2. 精确记忆：normalize(text) 签名命中学习库 -> 直接复用
  3. 相似记忆：动作词指纹高度相似 -> 复用
  4. 都没命中：调大模型判定 -> 结果写回学习库（下次同类直接命中）

模型超时/失败：返回 None，调用方降级到关键词路由。
"""
import json
import logging
import re
import urllib.request

log = logging.getLogger('kefu')

# 动作关键词指纹：用于相似匹配（中文词组）
_FINGERPRINT_TERMS = [
    '拦截', '召回', '退回寄件', '催件', '催派', '催发货', '催单', '揽收', '改地址', '送错地址',
    '没送到', '正确地址', '签收未收到', '没收到', '未收到', '少件', '少发', '少一盒', '少一瓶',
    '短少', '漏发', '破损', '压损', '污染', '提供核实', '核实结果', '发货视频', '打包视频',
    '称重图', '凭证', '签收证明', '特殊备注', '补发', '退货', '退回中', '拒收', '发票',
    '已录签收', '进村件', '拦截失败', '无法受理', '已通知网点', '已受理', '退回扫描',
    '改地址已处理', '催件已处理', '提供发货视频',
]

# 己方水消息/无意义回复
_FLUFF_PATTERNS = [
    r'^[0-9]{1,3}$',               # 1 / 11 / 666
    r'^[。.\s]+$',
    r'^(收到|好的|好|嗯|ok|OK|可以|明白|辛苦|谢谢|感谢|1|👍|对|是的)\s*[~!！。.]*$',
]


def normalize(text):
    """归一化消息：单号/长数字替换为 <NUM>，用于精确签名。
    <TRACKING_NO_E> 拦截 -> <NUM>拦截；JDVB68535680502 -> <NUM>"""
    t = str(text or '')
    t = re.sub(r'JD[VABEO]?[A-Z]?\d+', '<NUM>', t, flags=re.IGNORECASE)  # 京东单号
    re.sub(r'SF\d+', '<NUM>', t, flags=re.IGNORECASE)
    t = re.sub(r'\b\d{10,16}\b', '<NUM>', t)        # 中通/快递单号
    t = re.sub(r'ck[jh]?\d+', '<CK>', t, flags=re.IGNORECASE)  # 仓库单号
    t = re.sub(r'361\d{10,}', '<ORDER>', t)         # 平台订单号
    t = re.sub(r'1[3-9]\d{9}', '<PHONE>', t)        # 手机号
    t = re.sub(r'\s+', '', t)                       # 去空白
    t = re.sub(r'[，,。！!？?、~～\s]+', '', t)
    return t.strip()


def fingerprint(text):
    """提取动作词指纹（有序去重列表），用于相似匹配。"""
    t = str(text or '')
    found = []
    for term in _FINGERPRINT_TERMS:
        if term in t and term not in found:
            found.append(term)
    return tuple(sorted(found))


def is_fluff(text, sender, own_staff=None, is_robot=False):
    """快速判定为不需要路由的水消息/己方内部消息。返回 True 表示无需进一步处理。"""
    # <回写机器人名>等外部结果消息不走 fluff（需要判断回流）
    if is_robot:
        return False
    t = str(text or '').strip()
    if not t:
        return True
    for pat in _FLUFF_PATTERNS:
        if re.match(pat, t):
            return True
    return False


# ---------------- 大模型调用 ----------------

_SYSTEM_PROMPT = """你是电商客服微信群的消息路由助手。根据消息内容和发送人，判断该消息需要转发到哪些工作群。

【群/动作定义】
- B：需要中通快递处理的快递操作（拦截、召回、签收未收到、催件、催派、送错地址核实等）。仅当中通快递单号（13-14位、常以7开头）且是明确的快递操作指令时。
- C：催发货/需要尽快揽收发出。
- D：仓库发货核实类（客户反馈少件少发、要发货视频/打包视频/称重图、特殊备注订单的仓库处理）。
- A：中通"<回写机器人名>"反馈拦截失败（已签收/在代收点/进村件/未到达退改地址/无法受理），需回传客服群并@发单人。
- NONE：不需要转发。

【发送人规则】
1. 发送人是己方客服（昵称含"<公司简称>""<己方客服D>""<己方客服E>""<己方客服B>""<己方客服C>""<己方客服F>""<己方客服H>""<己方客服G>"等）：己方处理/搬运，输出 NONE。
2. 发送人"<回写机器人名>"：明确拦截失败才 ["A"]；正常"已通知网点/已受理/退回扫描已处理完成/已送达本人"输出 NONE。

【必须判 NONE 的易错情形（重点）】
- 顺丰单号（SF开头）的签收/退货/售后：不转中通B群，输出 NONE。
- "这个可以拒收么/能拒收吗"等是咨询，不是拦截指令 -> NONE。
- "签收 效果问题特殊退/特殊退/已使用"等售后退货结论 -> NONE。
- "退货验收/验收图片/退货验收情况/退回单号…验收"是平台退货验收，不是仓库少件核实 -> NONE。
- "截图一下出库数量/出库数量" -> NONE。
- "提供核实结果"若指中通快递核实（在中通群跟进的），NONE；仅当明确是仓库发货少件、要打包/发货视频时才 D。
- "退回中，已优先退款请7日内跟进"退货跟进话术 -> NONE。
- 开发票、退件验收、赠品送不送、咨询问答 -> NONE。

【正向规则】
3. 外部售后对中通单号发明确快递异常指令（拦截/召回/签收未收到/催件/送错地址）-> B。
4. 外部售后催发货（要快递尽快发出、催揽收、京东JDVB单号催发货）-> C。
5. 外部售后明确反馈少件少发、要发货视频/打包视频/称重图/核实是否漏发 -> D。
6. 一条消息可同时多目标（少件既要B快递核实又要D仓库视频则 ["B","D"]），但大多数只有一个；拿不准就 NONE。

只输出 JSON：{"routes": ["B"或"C"或"D"或"A"], "reason": "简短原因"}。没有目标就 {"routes": [], "reason": "..."}。"""


def _call_llm(sender, text, cfg, timeout=20):
    """调用火山方舟 ark-code-latest。返回 routes 列表；失败返回 None。"""
    api_key = cfg.get('api_key', '')
    base = cfg.get('base_url', 'https://ark.cn-beijing.volces.com/api/plan/v3').rstrip('/')
    model = cfg.get('model', 'ark-code-latest')
    if not api_key:
        return None
    user = f'发送人：{sender}\n消息内容：{text}\n请判断路由：'
    body = json.dumps({
        'model': model,
        'temperature': 0,
        'max_tokens': 200,
        'messages': [
            {'role': 'system', 'content': _SYSTEM_PROMPT},
            {'role': 'user', 'content': user},
        ],
    }).encode('utf-8')
    req = urllib.request.Request(
        f'{base}/chat/completions',
        data=body,
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        content = data['choices'][0]['message']['content']
        m = re.search(r'\{.*\}', content, re.S)
        if not m:
            return None
        result = json.loads(m.group(0))
        routes = result.get('routes', []) or []
        return [str(r).upper().replace('A回传', 'A') for r in routes]
    except Exception as e:
        log.warning('大模型意图识别失败（将降级关键词）: %s', str(e)[:80])
        return None


# ---------------- 学习库 ----------------

class IntentStore:
    """本地学习库：normalize 签名 / 指纹 -> 判定结果，持久化到 SQLite。"""

    def __init__(self, storage):
        self.db = storage.conn
        self._ensure_table()

    def _ensure_table(self):
        with self.db:
            self.db.execute(
                '''CREATE TABLE IF NOT EXISTS intent_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    norm TEXT UNIQUE,
                    fingerprint TEXT,
                    sender TEXT,
                    routes TEXT,
                    sample TEXT,
                    source TEXT,
                    hits INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                )''')
            self.db.execute('CREATE INDEX IF NOT EXISTS idx_intent_fp ON intent_memory(fingerprint)')

    def lookup_exact(self, norm):
        with self.db:
            row = self.db.execute('SELECT routes FROM intent_memory WHERE norm=?', (norm,)).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except Exception:
                return None
        return None

    def lookup_similar(self, fp):
        """指纹完全相同的复用历史结果。返回 routes 或 None。"""
        if not fp:
            return None
        fp_str = '|'.join(fp)
        with self.db:
            rows = self.db.execute(
                'SELECT routes,fingerprint,hits FROM intent_memory ORDER BY hits DESC LIMIT 20').fetchall()
        for routes_json, fp_db, hits in rows:
            if not fp_db:
                continue
            db_fp = set(fp_db.split('|'))
            cur = set(fp)
            # 指纹完全一致，或当前指纹被历史覆盖且动作词数相同 -> 复用
            if db_fp == cur and cur:
                try:
                    return json.loads(routes_json)
                except Exception:
                    continue
        return None

    def remember(self, norm, fp, sender, routes, sample, source='llm'):
        with self.db:
            self.db.execute(
                '''INSERT INTO intent_memory (norm, fingerprint, sender, routes, sample, source, hits)
                   VALUES (?,?,?,?,?,?,1)
                   ON CONFLICT(norm) DO UPDATE SET hits=hits+1, routes=excluded.routes''',
                (norm, '|'.join(fp), sender or '', json.dumps(routes, ensure_ascii=False),
                 (sample or '')[:200], source))

    def hit(self, norm):
        with self.db:
            self.db.execute('UPDATE intent_memory SET hits=hits+1 WHERE norm=?', (norm,))

    def stats(self):
        with self.db:
            total = self.db.execute('SELECT COUNT(*) FROM intent_memory').fetchone()[0]
            llm = self.db.execute("SELECT COUNT(*) FROM intent_memory WHERE source='llm'").fetchone()[0]
            hits = self.db.execute('SELECT COALESCE(SUM(hits),0) FROM intent_memory').fetchone()[0]
        return {'memory_total': total, 'llm_learned': llm, 'cache_hits': hits}


def classify(sender, text, own_staff, cfg, intent_store, is_robot=False):
    """综合判定一条消息的路由。
    返回 (routes:list[str], source:str)。
      routes: 如 ['B'] / ['B','D'] / [] (NONE)
      source: 'fluff'|'cache_exact'|'cache_similar'|'llm'|'llm_fail'
    大模型失败时返回 (None,'llm_fail')，调用方降级关键词。
    """
    # 1. 快速预筛
    if is_fluff(text, sender, own_staff=own_staff, is_robot=is_robot):
        return [], 'fluff'
    norm = normalize(text)
    fp = fingerprint(text)

    # 2. 精确记忆
    cached = intent_store.lookup_exact(norm)
    if cached is not None:
        intent_store.hit(norm)
        return cached, 'cache_exact'

    # 3. 相似记忆
    sim = intent_store.lookup_similar(fp)
    if sim is not None and fp:
        intent_store.remember(norm, fp, sender, sim, text, source='cache_similar')
        return sim, 'cache_similar'

    # 4. 调大模型
    llm_cfg = cfg or {}
    routes = _call_llm(sender, text, llm_cfg)
    if routes is None:
        return None, 'llm_fail'
    routes = routes or []
    intent_store.remember(norm, fp, sender, routes, text, source='llm')
    return routes, 'llm'

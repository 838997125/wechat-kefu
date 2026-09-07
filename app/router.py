# -*- coding: utf-8 -*-
"""多群消息路由引擎：源群消息 → 匹配路由 → 生成转发内容。"""
import re

# 单号提取：京东 JD 开头单号、中通 14 位（7开头）快递单号
_TRACKING_PATTERNS = [
    re.compile(r'JDV[AB]?\d{10,13}', re.IGNORECASE),   # 京东快递单号 JDVB/JDVA
    re.compile(r'JD[VOA]?\d{10,13}', re.IGNORECASE),   # 京东单号 JDV/JDO/JDA
    re.compile(r'(?<!\d)7[0-9]\d{12}(?!\d)'),          # 中通 14 位（70-79 开头）
    # 注：不做 12~16 位纯数字兜底，避免把京喜/平台订单号（如 36104…16 位）误判为快递单号
]

# 提取消息中所有快递单号（用于催发货等可能含多个单号的消息）
_ALL_TRACKING = [
    re.compile(r'JDV[AB]?\d{10,13}', re.IGNORECASE),
    re.compile(r'JD[VOA]?\d{10,13}', re.IGNORECASE),
    re.compile(r'(?<!\d)7[0-9]\d{12}(?!\d)'),
]


def _norm(tok):
    return tok.upper() if tok.upper().startswith('JD') else tok


def extract_tracking_no(text):
    """提取第一个快递单号；没有则空串。"""
    for pat in _TRACKING_PATTERNS:
        m = pat.search(text or '')
        if m:
            return _norm(m.group(0))
    return ''


def extract_all_tracking(text):
    """提取消息中全部快递单号（去重、保序）。"""
    out = []
    for pat in _ALL_TRACKING:
        for m in pat.finditer(text or ''):
            tok = _norm(m.group(0))
            if tok not in out:
                out.append(tok)
    return out


def is_own_staff(sender, own_staff=None):
    """是否为己方客服：昵称在名单内或含名单关键词。"""
    s = sender or ''
    if not s:
        return False
    for kw in (own_staff or []):
        if kw and kw in s:
            return True
    return False


def _sender_match(sender, require_contains, exclude_contains):
    """发送者昵称是否满足包含/排除条件。"""
    s = sender or ''
    for kw in (exclude_contains or []):
        if kw and kw in s:
            return False
    for kw in (require_contains or []):
        if kw and kw not in s:
            return False
    return True


def _text_match(text, keywords, regex):
    """内容匹配：regex 优先，否则关键词包含任一。"""
    if regex:
        try:
            if re.search(regex, text):
                return True
        except re.error:
            pass
    low = text.lower()
    return any(str(k).lower() in low for k in (keywords or []))


def _summarize(text):
    """<回写机器人名>结果消息提炼要点：去掉内部 @<己方客服E> 等前缀，截取核心。"""
    t = (text or '').strip()
    # 去掉开头的 @某人（含特殊空格 u2005）
    t = re.sub(r'^@\S+[\s\u2005\u00a0]*', '', t)
    # 去掉单号（模板里单独放）
    t = re.sub(r'(?<!\d)\d{12,16}(?!\d)', '', t)
    t = re.sub(r'\s{2,}', ' ', t).strip(' ，。:：')
    # 截断过长内容
    return t[:120]


def match_route(routes, source_chat, sender, content, mtype):
    """返回命中的路由 dict；不命中返回 None。
    只处理文本/引用类消息，群名必须与 source 完全一致。
    """
    if mtype not in ('text', 'quote'):
        return None
    for rt in routes:
        if not rt.get('enabled', True):
            continue
        if rt.get('source') != source_chat:
            continue
        if not _sender_match(sender, rt.get('require_sender_contains'), rt.get('exclude_sender_contains')):
            continue
        if not _text_match(content, rt.get('keywords'), rt.get('regex')):
            continue
        # 排除关键词：命中任一则该路由不生效
        if any(k and k in (content or '') for k in (rt.get('exclude_keywords') or [])):
            continue
        if _text_match(content, rt.get('keywords'), rt.get('regex')):
            return rt
    return None


def match_routes(routes, source_chat, sender, content, mtype, own_staff=None):
    """返回所有命中的路由列表（一条消息可同时转发到多个目标群）。"""
    if mtype not in ('text', 'quote'):
        return []
    # 己方客服发的消息不触发任何转发（<回写机器人名>等外部机器人除外）
    if own_staff is not None and is_own_staff(sender, own_staff) and '<回写机器人名>' not in (sender or ''):
        return []
    out = []
    for rt in routes:
        if not rt.get('enabled', True):
            continue
        if rt.get('source') != source_chat:
            continue
        if not _sender_match(sender, rt.get('require_sender_contains'), rt.get('exclude_sender_contains')):
            continue
        if not _text_match(content, rt.get('keywords'), rt.get('regex')):
            continue
        # 排除关键词：命中任一则该路由不生效
        if any(k and k in (content or '') for k in (rt.get('exclude_keywords') or [])):
            continue
        out.append(rt)
    return out


def _result_summary(text):
    """<回写机器人名>“无法拦截”类结果提炼要点：按具体失败原因返回简短结论。"""
    t = text or ''
    if any(k in t for k in ['进村件', '投递至村站', '完成转运派送', '二段物流', '转运仓']):
        return '快件已投进村站/转运，无法拦截退回'
    if '拦截失败' in t or '未到达指定退改地址' in t or '未到达指定退' in t:
        return '拦截失败，快件未到达退改地址'
    if any(k in t for k in ['已录签收', '已签收', '代收点', '驿站', '已取件', '已被取走', '已完成签收']):
        return '快件已签收/在代收点，无法拦截退回'
    if '无法受理' in t:
        return '网点已无法受理拦截'
    return '拦截失败，请发单客服知悉处理'


def build_forward(route, msg_content, sender, tracking_no, requester=None, all_numbers=None):
    """根据路由 mode 生成最终要发到目标群的文本。
    raw：纯原文；process：用 template 加工。
    requester：源单号在目标群（A群）的发单人昵称，用于 @。
    all_numbers：消息中提取到的全部快递单号（催发货等多单号场景）。
    """
    content = msg_content
    if route.get('mode') == 'process':
        tpl = route.get('template') or '【通知】单号{单号}：{来源摘要}'
        numbers_str = ' / '.join(all_numbers or ([tracking_no] if tracking_no else []))
        if route.get('extract_numbers'):
            # 催发货类：只发快递单号，不带原始长文本；未识别到快递单号则返回 None（不转发）
            if not all_numbers and not tracking_no:
                return None
            summary = '催发货，请安排优先发出'
        else:
            summary = _result_summary(content) if route.get('at_requester') else _summarize(content)
        out = (tpl
               .replace('{单号}', tracking_no or '未知')
               .replace('{全部单号}', numbers_str or '（未识别到快递单号）')
               .replace('{来源摘要}', summary)
               .replace('{text}', summary)
               .replace('{sender}', sender or '')
               .replace('{@发单人}', ('@' + requester + ' ') if requester else ''))
        # @发单人由发送通道的 at 参数完成（避免文本@与at参数重复）
        return out.strip()
    # raw：纯原文（去掉引用消息可能带的换行干扰，保持原样）
    return content.strip()


def route_dedup_key(route, msg_content, all_numbers=None):
    """转发去重 key：按单号去重的路由用全部单号拼key（同一批单只转一次）；
    否则用消息原文。
    """
    if route.get('dedup_by_number') and all_numbers:
        return '|'.join(sorted(all_numbers))
    return (msg_content or '').strip()

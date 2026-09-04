# -*- coding: utf-8 -*-
"""规则引擎：关键词匹配 + @ 解析 + 回复动作生成。"""
import random
import re


def is_bot_mentioned(content, bot_names):
    return any(f'@{n}' in (content or '') for n in bot_names)


def strip_bot_mentions(content, bot_names):
    text = content or ''
    for n in bot_names:
        text = text.replace(f'@{n}', '')
    text = re.sub(r'^[\s　\u2005\u00a0]+', '', text)
    return text.strip()


def _match_one(rule, text, mentioned):
    if rule.get('require_at') and not mentioned:
        return False
    kws = rule.get('keywords', []) or []
    if not kws:
        return False
    mode = rule.get('match', 'any')
    if mode == 'exact':
        return text in kws or text.lower() in [str(k).lower() for k in kws]
    if mode == 'regex':
        try:
            return any(re.search(str(k), text) for k in kws)
        except re.error:
            return False
    if mode == 'all':
        return all(str(k).lower() in text.lower() for k in kws)
    # any（包含）
    return any(str(k).lower() in text.lower() for k in kws)


def _scope_ok(rule, chat_name, chat_type):
    scope = rule.get('scope', 'all')
    if scope == 'group' and chat_type != 'group':
        return False
    if scope == 'dm' and chat_type != 'dm':
        return False
    chats = rule.get('chats', []) or []
    if chats and chat_name not in chats:
        return False
    return True


def match_rule(rules, chat_name, chat_type, text, mentioned):
    """返回命中的规则 dict，否则 None。"""
    for rule in rules:
        if not rule.get('enabled', True):
            continue
        if not _scope_ok(rule, chat_name, chat_type):
            continue
        if _match_one(rule, text, mentioned):
            return rule
    return None


def resolve_at(at_cfg, sender_name, chat_type):
    """把规则的 at 配置解析为 SendMsg 需要的昵称列表。
    'asker' → @提问人；'none'/空 → 不@；其他字符串 → 固定 @某人。
    """
    if not at_cfg:
        return None
    names = []
    for item in at_cfg:
        if item == 'asker':
            if chat_type == 'group' and sender_name:
                names.append(sender_name)
        elif item in ('none', '', None):
            continue
        else:
            names.append(str(item))
    return names or None


def render_replies(rule, sender_name):
    """规则回复支持 {sender} 占位符；返回全部回复文本列表。"""
    out = []
    for r in (rule.get('replies', []) or []):
        out.append(str(r).replace('{sender}', sender_name or '亲'))
    return [t for t in out if t.strip()]


def choose_replies(rule, sender_name):
    """按 reply_mode 决定实际发送的回复：
    random（默认，防风控）：多条话术随机回一条；all：全部连发。
    """
    rendered = render_replies(rule, sender_name)
    if not rendered:
        return []
    if rule.get('reply_mode', 'random') == 'all' or len(rendered) == 1:
        return rendered
    return [random.choice(rendered)]


def split_text(text, max_len):
    """长消息按段落/长度拆条。"""
    if len(text) <= max_len:
        return [text]
    parts = []
    buf = ''
    for line in text.split('\n'):
        if len(buf) + len(line) + 1 > max_len and buf:
            parts.append(buf)
            buf = ''
        while len(line) > max_len:
            if buf:
                parts.append(buf)
                buf = ''
            parts.append(line[:max_len])
            line = line[max_len:]
        buf = (buf + '\n' + line) if buf else line
    if buf:
        parts.append(buf)
    return parts

# -*- coding: utf-8 -*-
"""
大模型客户端（OpenAI 兼容 /chat/completions）。

用户在面板里自备接口：DeepSeek、通义、Kimi、GPT、任意 OpenAI 兼容网关均可。
每个会话独立保留最近 N 轮上下文（内存态，重启清空）。
"""
import json
import logging
import threading
import urllib.error
import urllib.request
from collections import deque

log = logging.getLogger('kefu')


class AIClient:
    def __init__(self):
        self._history = {}          # session_key -> deque[[role, content]]
        self._lock = threading.Lock()

    def chat(self, ai_cfg, session_key, chat_name, sender_name, text):
        """同步调用，返回回复文本；失败抛异常。"""
        url = ai_cfg.get('base_url', '').rstrip('/') + '/chat/completions'
        api_key = ai_cfg.get('api_key', '')
        model = ai_cfg.get('model', '')
        timeout = float(ai_cfg.get('timeout_sec', 60))
        memory_turns = int(ai_cfg.get('memory_turns', 10) or 0)

        sys_prompt = ai_cfg.get('system_prompt', '') or '你是一名微信客服助手。'
        user_tpl = ai_cfg.get('user_prompt') or '客户消息：{text}'
        user_content = user_tpl.format(chat=chat_name, sender=sender_name, text=text)

        messages = [{'role': 'system', 'content': sys_prompt}]
        with self._lock:
            hist = self._history.setdefault(session_key, deque(maxlen=max(memory_turns * 2, 0)))
            messages.extend([{'role': r, 'content': c} for r, c in hist])
            messages.append({'role': 'user', 'content': user_content})

        body = {
            'model': model,
            'messages': messages,
            'temperature': float(ai_cfg.get('temperature', 0.7) or 0.7),
            'stream': False,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
            headers={
                'Content-Type': 'application/json; charset=utf-8',
                'Authorization': f'Bearer {api_key}',
            },
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        reply = (data.get('choices') or [{}])[0].get('message', {}).get('content', '').strip()
        if not reply:
            raise RuntimeError('模型返回为空: %s' % json.dumps(data, ensure_ascii=False)[:200])

        if memory_turns > 0:
            with self._lock:
                hist.append(['user', user_content])
                hist.append(['assistant', reply])
        return reply

    def reset(self, session_key=None):
        with self._lock:
            if session_key:
                self._history.pop(session_key, None)
            else:
                self._history.clear()

    def test(self, ai_cfg, prompt='你好，请用一句话回复确认你工作正常。'):
        """面板「测试连接」用。"""
        return self.chat(dict(ai_cfg, memory_turns=0), '__test__', '测试', '测试员', prompt)

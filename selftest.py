# -*- coding: utf-8 -*-
"""客服微信助手 - 离线集成自测（不连接微信，用假驱动验证 bot 全流程）。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8')

from app.config import Config
from app.storage import Storage
from app.bot import Bot
from app.driver import NormalizedMsg

PASS, FAIL = 0, 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  ✅ {name}')
    else:
        FAIL += 1
        print(f'  ❌ {name} {detail}')


class FakeDriver:
    def __init__(self):
        self.ready = True
        self.account = {'display_name': 'testbot'}
        self.sent = []

    def online(self):
        return True

    def send(self, chat, text, at=None):
        self.sent.append((chat, text, at))
        return True


def main():
    tmp = tempfile.mkdtemp(prefix='kefu_selftest_')
    cfg = Config(os.path.join(tmp, 'config.json'))
    cfg.save({
        'general': {
            'bot_names': ['小助'], 'poll_interval_sec': 0.1, 'switch_wait_sec': 0.1,
            'reply_delay_min': 0, 'reply_delay_max': 0, 'max_replies_per_minute': 100,
            'split_length': 100, 'record_self': True, 'at_sender': True, 'auto_open_panel': False,
        },
        'chats': [{'name': '测试群', 'type': 'group', 'enabled': True}],
        'rules': [
            {'id': 'r1', 'name': '查快递', 'enabled': True, 'scope': 'all', 'chats': [],
             'keywords': ['快递', '单号'], 'match': 'any', 'require_at': False,
             'replies': ['亲~请把【订单号】发在群里，客服马上核实 {sender}'], 'at': ['asker']},
            {'id': 'r2', 'name': '下班时间', 'enabled': True, 'scope': 'all', 'chats': [],
             'keywords': ['下班'], 'match': 'any', 'require_at': False,
             'replies': ['客服 21 点下班哦'], 'at': ['none']},
            {'id': 'r3', 'name': '长回复', 'enabled': True, 'scope': 'all', 'chats': [],
             'keywords': ['长文'], 'match': 'any', 'require_at': False,
             'replies': ['A' * 130], 'at': ['none']},
        ],
        'welcome': {'enabled': False, 'chats': [], 'message': '欢迎 {name} 进群~', 'at_newcomer': True},
        'ai': {'enabled': False, 'trigger': 'at', 'base_url': '', 'api_key': '', 'model': '',
               'temperature': 0.7, 'timeout_sec': 10, 'memory_turns': 0,
               'system_prompt': '', 'user_prompt': '{text}'},
    })
    storage = Storage(os.path.join(tmp, 'messages.db'))
    bot = Bot(cfg, storage)
    fake = FakeDriver()
    bot.driver = fake

    print('1) 关键词命中 + @提问人:')
    bot._on_message(NormalizedMsg('测试群', 'group', '<己方客服H>', 'friend', 'text', '帮我查个快递'))
    check('发出 1 条回复', len(fake.sent) == 1, str(fake.sent))
    check('回复内容正确', fake.sent and '订单号' in fake.sent[0][1], fake.sent[0][1] if fake.sent else '')
    check('@了提问人<己方客服H>', fake.sent and fake.sent[0][2] == ['<己方客服H>'], str(fake.sent[0][2] if fake.sent else None))
    check('{sender} 占位符替换', fake.sent and '<己方客服H>' in fake.sent[0][1])

    print('2) 关键词命中 + 不@:')
    n = len(fake.sent)
    bot._on_message(NormalizedMsg('测试群', 'group', '<己方客服E>', 'friend', 'text', '你们几点下班'))
    check('发出回复', len(fake.sent) == n + 1)
    check('at 为 None', fake.sent[-1][2] is None, str(fake.sent[-1][2]))

    print('3) 群里未 @ 且无关键词 -> 不回复:')
    n = len(fake.sent)
    bot._on_message(NormalizedMsg('测试群', 'group', '<己方客服E>', 'friend', 'text', '今天天气真好啊'))
    check('未发回复', len(fake.sent) == n)

    print('4) 自己发的消息不回复:')
    n = len(fake.sent)
    bot._on_message(NormalizedMsg('测试群', 'group', '小助', 'self', 'text', '帮我查个快递'))
    check('未发回复', len(fake.sent) == n)

    print('5) 长消息拆条:')
    n = len(fake.sent)
    bot._on_message(NormalizedMsg('测试群', 'group', '<己方客服H>', 'friend', 'text', '来段长文'))
    check('拆成 2 段发送', len(fake.sent) - n == 2, f'实际 {len(fake.sent)-n}')

    print('6) 新人欢迎语（默认关闭 -> 开启）:')
    n = len(fake.sent)
    bot._on_message(NormalizedMsg('测试群', 'group', 'system', 'system', 'system', '小王 加入群聊'))
    check('关闭时不发欢迎语', len(fake.sent) == n)
    d = cfg.data
    d['welcome']['enabled'] = True
    cfg.save(d)
    cfg.load(force=True)
    bot._on_message(NormalizedMsg('测试群', 'group', 'system', 'system', 'system', '小赵, 小孙 加入群聊'))
    check('开启后发欢迎语', len(fake.sent) == n + 1, str(fake.sent[-1] if fake.sent else ''))
    check('欢迎语 @ 新成员', fake.sent and fake.sent[-1][2] == ['小赵', '小孙'], str(fake.sent[-1][2] if fake.sent else None))

    print('7) AI 兜底（@机器人，规则未命中）:')
    d = cfg.data
    d['ai'].update({'enabled': True, 'api_key': 'sk-fake', 'trigger': 'at'})
    cfg.save(d)
    cfg.load(force=True)
    bot.ai.chat = lambda ai_cfg, sk, chat, sender, text: f'AI回复: 你好{sender}'
    n = len(fake.sent)
    bot._on_message(NormalizedMsg('测试群', 'group', '<己方客服E>', 'friend', 'text', '@小助 这个能优惠吗'))
    check('AI 回复已发送', len(fake.sent) == n + 1, str(fake.sent[n:] if len(fake.sent) > n else ''))
    check('AI 回复内容正确', fake.sent and fake.sent[-1][1].startswith('AI回复'), fake.sent[-1][1] if fake.sent else '')
    check('AI 群回复 @ 提问人', fake.sent and fake.sent[-1][2] == ['<己方客服E>'], str(fake.sent[-1][2] if fake.sent else None))
    n2 = len(fake.sent)
    bot._on_message(NormalizedMsg('测试群', 'group', '<己方客服E>', 'friend', 'text', '没@的闲聊'))
    check('未 @ 不走 AI', len(fake.sent) == n2)

    print('8) 消息落库:')
    rows = storage.recent(limit=50)
    check('消息已记录', len(rows) >= 8, f'{len(rows)} 条')
    replied = [r for r in rows if r['replied']]
    check('回复记录可追溯', len(replied) >= 5, f'{len(replied)} 条 replied')

    print(f'\n结果：{PASS} 通过 / {FAIL} 失败')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

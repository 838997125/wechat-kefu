# -*- coding: utf-8 -*-
"""真机实弹测试：真实 wxauto 驱动读群消息 + 规则引擎 + 向真实群发送两条回复（带 @）。
验证免费版 wxauto4 在微信 4.1.8.107 上的「群发送 + @成员」能力（此前唯一未真机验证的环节）。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8')

from app.config import Config
from app.driver import WxDriver
from app import rules as R

# 测试目标可用环境变量覆盖：KEFU_TEST_GROUP（群名）、KEFU_TEST_AT（@的群成员昵称）
GROUP = os.environ.get('KEFU_TEST_GROUP', '客服测试群')
AT_WHO = os.environ.get('KEFU_TEST_AT', '测试成员')

cfg = Config(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json'))
driver = WxDriver(cfg.general())
driver.start()

print('\n=== 1) 真机读取群消息（接收链路） ===')
msgs = driver.poll([{'name': GROUP, 'type': 'group', 'enabled': True}])
friends = [m for m in msgs if m.attr == 'friend']
print(f'读取到消息 {len(msgs)} 条，其中群友消息 {len(friends)} 条')
for m in friends[-3:]:
    print(f'  - {m.sender}: {m.content[:40]}')

print('\n=== 2) 规则引擎（取第一条启用规则） ===')
rules = cfg.rules()
assert rules, 'config.json 中没有启用的规则，请先在面板或配置里添加规则'
rule = rules[0]
replies = R.render_replies(rule, AT_WHO)
at_list = R.resolve_at(rule.get('at'), AT_WHO, 'group')
print(f'使用规则: {rule["name"]}')
print(f'回复 {len(replies)} 条, @对象: {at_list}')

print('\n=== 3) 真机发送到群（第1条带 @） ===')
ok1 = driver.send(GROUP, replies[0], at=at_list)
print('第1条发送结果:', ok1)
ok2 = True
if len(replies) > 1:
    time.sleep(2.5)
    ok2 = driver.send(GROUP, replies[1], at=None)
    print('第2条发送结果:', ok2)
time.sleep(2.5)

print('\n=== 4) 回读确认（直接读当前群窗口，poll 的基线机制会跳过无未读会话） ===')
time.sleep(1.5)
driver.wx.ChatWith(GROUP)
time.sleep(1.5)
raw = driver.wx.GetAllMessage()
self_msgs = [str(getattr(m, 'content', '')) for m in raw if getattr(m, 'attr', '') == 'self']
print(f'回读到 self 消息 {len(self_msgs)} 条，最后一条: {self_msgs[-1][:60]!r}' if self_msgs else '未回读到 self 消息')
# 校验：每条回复的前 10 个字都出现在最近的 self 消息中；第1条带 @
joined = '\n'.join(self_msgs[-len(replies) - 2:])
reply_ok = all(r[:10] in joined for r in replies)
at_ok = (at_list is None) or (f'@{AT_WHO}' in joined)
ok = ok1 and ok2 and reply_ok and at_ok

print('\n' + ('✅ 真机实弹测试通过：群消息读取 + 规则匹配 + 群发送 + @成员 全链路成功' if ok
              else '❌ 实弹测试有环节失败，请检查上面的输出'))
sys.exit(0 if ok else 1)

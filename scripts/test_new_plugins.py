#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新插件离线自检：防炸群 / 群事件监听

用假 ctx 跑核心逻辑，不需要真机器人、不需要数据库、不需要联网。

用法：python scripts/test_new_plugins.py
"""
import asyncio
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
    else:
        FAIL.append(f'{name} {extra}')


def load(name):
    path = os.path.join(ROOT, 'plugins', name, 'main.py')
    spec = importlib.util.spec_from_file_location(f'_plug_{name}', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f'_plug_{name}'] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeServices:
    def __init__(self, can_mute=True):
        self._can = can_mute
        self.primary = types.SimpleNamespace(
            supports=lambda cap, direction='out': (cap == 'group_admin') and self._can)

    def adapter_for_source(self, src):
        return None

    def primary_adapter(self):
        return self.primary


class FakeCtx:
    """够跑插件逻辑的最小 ctx"""

    def __init__(self, can_mute=True, config=None):
        self.sent = []
        self.mutes = []
        self.whole_bans = []
        self.logs = []
        self.subscribed = []
        self.commands = []
        self._config = config or {}
        self._framework = types.SimpleNamespace(services=FakeServices(can_mute))
        self._tables = []

    # 配置
    def get_config(self, key, default=None):
        return self._config.get(key, default)

    # 日志 / 注册
    def log(self, msg, level='info'):
        self.logs.append(msg)

    def on(self, event_name, handler):
        self.subscribed.append(event_name)

    def on_raw_message(self, handler):
        self.raw_handler = handler

    def command(self, pattern, handler, **kw):
        self.commands.append(pattern)

    def dashboard_card(self, title, handler, **kw):
        pass

    def task(self, cron, handler, **kw):
        pass

    def create_table(self, ddl):
        self._tables.append(ddl)

    # 数据库：全部空结果，插件会退回默认值
    def db_query_one(self, sql, params=None):
        return None

    def db_execute(self, sql, params=None):
        return 1

    # 动作
    async def asend_msg(self, group_id=None, user_id=None, message=None, **kw):
        self.sent.append({'group_id': group_id, 'user_id': user_id,
                          'message': message})

    async def aban(self, group_id, user_id, duration, **kw):
        self.mutes.append((group_id, user_id, duration))

    async def amute_all(self, group_id, enable, **kw):
        self.whole_bans.append((group_id, enable))


def run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────
# 防炸群
# ─────────────────────────────────────────────────────────────

def test_antispam_register():
    mod = load('anti_spam')
    ctx = FakeCtx()
    mod.register(ctx)
    check('防炸群 建表', len(ctx._tables) == 1)
    check('防炸群 注册原始消息处理器', hasattr(ctx, 'raw_handler'))
    check('防炸群 注册命令', len(ctx.commands) >= 3, ctx.commands)


def test_antispam_user_flood():
    """单人窗口内刷够条数 → 禁言一次，且不会重复处置"""
    mod = load('anti_spam')
    ctx = FakeCtx()
    mod.register(ctx)
    mod._settings_cache.clear()
    mod._user_hits.clear()
    mod._muted_until.clear()

    cfg = mod._get_settings(10001)
    need = cfg['user_threshold']

    raw = {'post_type': 'message', 'message_type': 'group',
           'group_id': 10001, 'user_id': 555, 'message': 'x'}
    for _ in range(need + 2):
        run(ctx.raw_handler(dict(raw), 'bot'))

    check('单人刷屏触发禁言', len(ctx.mutes) == 1, ctx.mutes)
    if ctx.mutes:
        check('禁言时长取配置', ctx.mutes[0][2] == cfg['user_mute'], ctx.mutes)
        check('禁言对象正确', ctx.mutes[0][1] == 555, ctx.mutes)
    check('处置后发提示', any('禁言' in (s.get('message') or '')
                          for s in ctx.sent), ctx.sent)


def test_antispam_group_flood():
    """整群短时间涌入 → 全体禁言"""
    mod = load('anti_spam')
    ctx = FakeCtx()
    mod.register(ctx)
    mod._settings_cache.clear()
    mod._group_hits.clear()
    mod._group_muted_until.clear()
    mod._muted_until.clear()

    cfg = mod._get_settings(20002)
    need = cfg['group_threshold']
    for i in range(need + 2):
        raw = {'post_type': 'message', 'message_type': 'group',
               'group_id': 20002, 'user_id': 900 + i, 'message': 'x'}
        run(ctx.raw_handler(raw, 'bot'))

    check('群体刷屏触发全体禁言',
          any(e[1] is True for e in ctx.whole_bans), ctx.whole_bans)


def test_antispam_unsupported_adapter():
    """接入端不支持禁言 → 只告警不动手，不假装成功"""
    mod = load('anti_spam')
    ctx = FakeCtx(can_mute=False)
    mod.register(ctx)
    mod._settings_cache.clear()
    mod._user_hits.clear()
    mod._muted_until.clear()

    cfg = mod._get_settings(30003)
    raw = {'post_type': 'message', 'message_type': 'group',
           'group_id': 30003, 'user_id': 777, 'message': 'x'}
    for _ in range(cfg['user_threshold'] + 1):
        run(ctx.raw_handler(dict(raw), 'bot'))

    check('不支持禁言时不调用动作', ctx.mutes == [], ctx.mutes)


def test_antispam_ignores_private():
    mod = load('anti_spam')
    ctx = FakeCtx()
    mod.register(ctx)
    mod._user_hits.clear()
    raw = {'post_type': 'message', 'message_type': 'private',
           'user_id': 1, 'message': 'x'}
    for _ in range(50):
        run(ctx.raw_handler(dict(raw), 'bot'))
    check('私聊不进入刷屏统计', ctx.mutes == [] and ctx.whole_bans == [])


# ─────────────────────────────────────────────────────────────
# 群事件监听
# ─────────────────────────────────────────────────────────────

def test_groupwatch_register():
    mod = load('group_watch')
    ctx = FakeCtx()
    mod.register(ctx)
    check('群监听 建表', len(ctx._tables) == 1)
    wanted = {'notice.group_member_increase', 'notice.group_member_decrease',
              'notice.group_admin', 'notice.group_ban',
              'notice.message_recall', 'notice.poke'}
    check('订阅规范通知名', wanted.issubset(set(ctx.subscribed)), ctx.subscribed)
    check('群监听 注册命令', len(ctx.commands) >= 3, ctx.commands)


def test_groupwatch_join_notice():
    mod = load('group_watch')
    ctx = FakeCtx()
    mod.register(ctx)
    mod._cache.clear()

    handler = None
    # 找到订阅回调（register 时 ctx.on 存的是同一个函数）
    handler = mod._on_notice
    run(handler({'notice_type': 'group_increase',
                 'notice_type_canonical': 'group_member_increase',
                 'group_id': 40004, 'user_id': 111, 'nickname': '小明'}))
    check('进群提示发出', len(ctx.sent) == 1, ctx.sent)
    if ctx.sent:
        check('进群文案含昵称', '小明' in ctx.sent[0]['message'], ctx.sent)


def test_groupwatch_recall_and_ban():
    mod = load('group_watch')
    ctx = FakeCtx()
    mod.register(ctx)
    mod._cache.clear()

    run(mod._on_notice({'notice_type': 'group_msg_recall',
                        'notice_type_canonical': 'message_recall',
                        'group_id': 40004, 'user_id': 222,
                        'message_id': 9988}))
    run(mod._on_notice({'notice_type': 'group_ban',
                        'notice_type_canonical': 'group_ban',
                        'group_id': 40004, 'user_id': 333, 'duration': 600,
                        'operator_id': 444}))
    texts = ' | '.join(s['message'] for s in ctx.sent)
    check('撤回提示', '撤回' in texts, texts)
    check('禁言提示含时长', '600' in texts, texts)
    check('显示操作者', '444' in texts, texts)


def test_groupwatch_poke_off_by_default():
    mod = load('group_watch')
    ctx = FakeCtx()
    mod.register(ctx)
    mod._cache.clear()
    run(mod._on_notice({'notice_type': 'poke', 'notice_type_canonical': 'poke',
                        'group_id': 40004, 'user_id': 1, 'target_id': 2}))
    check('戳一戳默认关闭', ctx.sent == [], ctx.sent)


def test_groupwatch_unknown_notice_ignored():
    mod = load('group_watch')
    ctx = FakeCtx()
    mod.register(ctx)
    mod._cache.clear()
    run(mod._on_notice({'notice_type': 'weird_thing',
                        'notice_type_canonical': 'weird_thing',
                        'group_id': 40004}))
    check('未知通知不提示', ctx.sent == [], ctx.sent)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in tests:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            FAIL.append(f'{fn.__name__} 抛异常: {e}\n{traceback.format_exc()}')
    for n in PASS:
        print(f'  PASS  {n}')
    for n in FAIL:
        print(f'  FAIL  {n}')
    print(f'\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

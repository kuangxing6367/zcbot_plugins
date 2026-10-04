# -*- coding: utf-8 -*-
"""
群事件监听插件（group_watch）

把"群里发生了什么但没人看见"的事主动说出来：管理员变动、成员进出、
禁言、消息撤回。

为什么要这个插件
----------------
这些事在协议里都是**通知事件**（不是消息），默认谁也不会收到。要拿就得
翻接入端的原始字典，换个接入端字段名又不一样。本插件订阅的是框架的
**规范通知名**，OneBot / QQ 官方 / 别的接入端进来都能用。

订阅的事件（框架在内核里同时广播协议原名与规范名）：
    notice.group_member_increase   成员增加
    notice.group_member_decrease   成员减少（退群/被踢/机器人被移出）
    notice.group_admin             管理员变动
    notice.group_ban               禁言
    notice.message_recall          消息撤回
    notice.poke                    戳一戳

配置（_conf_schema.json，Web 面板可改）：
    各事件开关 + 是否显示操作者 + 是否只提示有管理员在场的群

命令：
    /watch            查看本群监听设置
    /watch 开关        开启/关闭本群监听
    /watch 撤回/进群/退群/管理/禁言/戳一戳 开关     单项开关
"""
import logging
import time

__plugin_meta__ = {
    "name": "群事件监听",
    "version": "1.1.0",
    "author": "ZGRIC",
    "desc": "管理员变动/成员进出/禁言/撤回等群事件主动提示，按群开关，昵称解析",
    "priority": 30,
}

logger = logging.getLogger('zcbot')

_TABLE = 'plugin_groupwatch_settings'

_DDL = """
CREATE TABLE IF NOT EXISTS plugin_groupwatch_settings (
    group_id      BIGINT PRIMARY KEY,
    enabled       INT DEFAULT 1,
    watch_join    INT DEFAULT 1,
    watch_leave   INT DEFAULT 1,
    watch_admin   INT DEFAULT 1,
    watch_ban     INT DEFAULT 1,
    watch_recall  INT DEFAULT 1,
    watch_poke    INT DEFAULT 0,
    show_operator INT DEFAULT 1,
    resolve_nick  INT DEFAULT 1,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# 旧版本表补列（已建表时 CREATE IF NOT EXISTS 不会新增列）
_MIGRATE = "ALTER TABLE plugin_groupwatch_settings ADD COLUMN resolve_nick INT DEFAULT 1"

# 规范通知名 → (开关字段名, 中文名)
_WATCHED = {
    'group_member_increase': 'watch_join',
    'group_member_decrease': 'watch_leave',
    'group_admin': 'watch_admin',
    'group_ban': 'watch_ban',
    'message_recall': 'watch_recall',
    'poke': 'watch_poke',
}

_LABEL = {
    'group_member_increase': '成员加入',
    'group_member_decrease': '成员离开',
    'group_admin': '管理员变动',
    'group_ban': '禁言',
    'message_recall': '消息撤回',
    'poke': '戳一戳',
}

_DEFAULTS = {
    "enabled": 1, "watch_join": 1, "watch_leave": 1, "watch_admin": 1,
    "watch_ban": 1, "watch_recall": 1, "watch_poke": 0, "show_operator": 1,
    "resolve_nick": 1,
}

# 昵称解析缓存: (group_id, user_id) -> (昵称, 过期时间戳)
_TTL = 30.0
_cache = {}
_name_cache = {}
_NAME_TTL = 600.0
_NAME_CACHE_MAX = 5000
_ctx = None
_cfg = {}                 # 插件配置（全局默认值，Web 面板可改）


def _load_cfg(ctx):
    global _cfg
    got = {}
    for key in _DEFAULTS:
        try:
            val = ctx.get_config(key, None)
        except Exception:       # noqa: BLE001
            val = None
        if val is not None:
            try:
                got[key] = int(val)
            except (TypeError, ValueError):
                pass
    _cfg = got


def _get_settings(group_id) -> dict:
    now = time.time()
    hit = _cache.get(group_id)
    if hit and hit[1] > now:
        return hit[0]
    settings = dict(_DEFAULTS)
    settings.update(_cfg)
    try:
        row = _ctx.db_query_one(
            f"SELECT * FROM {_TABLE} WHERE group_id=%s", (group_id,))
        if row:
            for k in _DEFAULTS:
                if row.get(k) is not None:
                    settings[k] = row[k]
    except Exception as e:      # noqa: BLE001
        logger.debug("group_watch 读取配置失败(%s): %s", group_id, e)
    _cache[group_id] = (settings, now + _TTL)
    return settings


def _save(group_id, patch: dict) -> bool:
    s = dict(_get_settings(group_id))
    s.update({k: v for k, v in patch.items() if v is not None})
    try:
        _ctx.db_execute(f"DELETE FROM {_TABLE} WHERE group_id=%s", (group_id,))
        _ctx.db_execute(
            f"INSERT INTO {_TABLE} (group_id, enabled, watch_join, watch_leave, "
            f"watch_admin, watch_ban, watch_recall, watch_poke, show_operator, "
            f"resolve_nick) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (group_id, s['enabled'], s['watch_join'], s['watch_leave'],
             s['watch_admin'], s['watch_ban'], s['watch_recall'],
             s['watch_poke'], s['show_operator'], s['resolve_nick']))
    except Exception as e:      # noqa: BLE001
        logger.error("group_watch 写入配置失败(%s): %s", group_id, e)
        return False
    _cache.pop(group_id, None)
    return True


async def _member_name(gid, user_id):
    """群成员昵称解析（群名片优先），失败回落 None；带 TTL 缓存"""
    key = (str(gid), str(user_id))
    now = time.time()
    hit = _name_cache.get(key)
    if hit:
        name, expire = hit
        if name and expire > now:
            return name
    name = None
    try:
        resp = await _ctx.aapi("get_group_member_info",
                               group_id=int(gid), user_id=int(user_id))
        info = resp.get("data") if isinstance(resp, dict) \
            and isinstance(resp.get("data"), dict) else resp
        if isinstance(info, dict):
            got = str(info.get("card") or info.get("nickname") or "").strip()
            if got:
                name = got
    except Exception as e:      # noqa: BLE001
        logger.debug("group_watch 昵称解析失败(%s/%s): %s", gid, user_id, e)
    if len(_name_cache) > _NAME_CACHE_MAX:
        _name_cache.clear()
    _name_cache[key] = (name, now + _NAME_TTL)
    return name


def _who(data: dict, *keys) -> str:
    """取人：优先昵称，退回 ID"""
    for k in keys:
        v = data.get(k)
        if v:
            return str(v)
    return ''


def _fmt(data: dict, canonical: str, settings: dict, names: dict) -> str:
    """把通知字典翻成人话（names 为已解析的昵称表 {str(uid): 昵称}）"""
    uid = data.get('user_id') or data.get('target_id') or ''
    op = data.get('operator_id') or data.get('operator') or ''
    sub = str(data.get('sub_type') or '')

    def _name(v, fallback=''):
        if not v:
            return fallback
        return names.get(str(v)) or str(fallback or v)

    if canonical == 'group_member_increase':
        who = _name(uid, _who(data, 'nickname', 'user_name') or uid)
        tail = '（管理员邀请）' if sub == 'invite' else ''
        base = f"{who} 加入了本群{tail}"
    elif canonical == 'group_member_decrease':
        who = _name(uid, _who(data, 'nickname', 'user_name') or uid)
        if sub == 'kick_me':
            base = "机器人被移出本群"
        elif sub in ('kick', 'kick_other'):
            base = f"{who} 被移出本群"
        else:
            base = f"{who} 退出了本群"
    elif canonical == 'group_admin':
        who = _name(uid, _who(data, 'nickname', 'user_name') or uid)
        if sub in ('unset', 'cancel', 'remove'):
            base = f"{who} 被取消了管理员"
        else:
            base = f"{who} 成为了管理员"
    elif canonical == 'group_ban':
        who = _name(uid, _who(data, 'nickname', 'user_name') or uid)
        dur = data.get('duration')
        if sub == 'lift_ban' or (isinstance(dur, int) and dur == 0):
            base = f"{who} 被解除禁言"
        elif dur:
            base = f"{who} 被禁言 {dur} 秒"
        else:
            base = f"{who} 被禁言"
    elif canonical == 'message_recall':
        who = _name(uid, _who(data, 'nickname', 'user_name') or uid)
        base = f"{who} 撤回了一条消息"
        if data.get('message_id'):
            base += f"（ID {data['message_id']}）"
    elif canonical == 'poke':
        who = _name(uid, _who(data, 'nickname', 'user_name') or uid)
        target = data.get('target_id') or ''
        base = f"{who} 戳了戳 {target}" if target else f"{who} 戳了戳"
    else:
        base = f"{_LABEL.get(canonical, canonical)}：{uid}"

    if settings.get('show_operator') and op and str(op) != str(uid):
        base += f"（操作者 {_name(op)}）"
    return base


async def _on_notice(data: dict):
    """
    通知事件入口。

    框架会同时广播协议原名与规范名，同一个事件可能触发两次——用
    `notice_type_canonical` 去重（没有该字段时退回 notice_type）。
    """
    if not isinstance(data, dict):
        return
    canonical = data.get('notice_type_canonical') \
        or data.get('notice_type') or ''
    if canonical not in _WATCHED:
        return
    gid = data.get('group_id')
    if not gid:
        return
    settings = _get_settings(gid)
    if not settings.get('enabled'):
        return
    if not settings.get(_WATCHED[canonical]):
        return
    try:
        names = {}
        if settings.get('resolve_nick'):
            names = {}
            for k in ('user_id', 'target_id', 'operator_id'):
                v = data.get(k)
                if v and str(v) not in ('', '0') and str(v) not in names:
                    name = await _member_name(gid, v)
                    if name:
                        names[str(v)] = name
        text = _fmt(data, canonical, settings, names)
        await _ctx.asend_msg(group_id=gid, message=f"[群事件] {text}")
    except Exception as e:      # noqa: BLE001
        logger.debug("group_watch 提示失败: %s", e)


# ── 命令 ───────────────────────────────────────────────────

def _is_admin(ev) -> bool:
    return ev.role in ('super', 'owner', 'admin')


async def cmd_status(ev, match):
    """查看本群事件监听设置"""
    if not ev.is_group:
        await _ctx.asend_msg(user_id=ev.user_id, message="该命令请在群内使用")
        return
    s = _get_settings(ev.group_id)
    lines = ["━━━━ 群事件监听 ━━━━",
             f"总开关：{'开启' if s['enabled'] else '关闭'}"]
    if s['enabled']:
        for canonical, field in _WATCHED.items():
            lines.append(f"{_LABEL[canonical]}：{'开启' if s[field] else '关闭'}")
        lines.append(f"显示操作者：{'是' if s['show_operator'] else '否'}")
        lines.append(f"解析昵称：{'是' if s['resolve_nick'] else '否'}")
    lines.append("━━━━━━━━━━━━━━━")
    lines.append("改：/watch 撤回 关    /watch 开关")
    await _ctx.asend_msg(group_id=ev.group_id, message="\n".join(lines))


async def cmd_toggle(ev, match):
    """/watch 开关 —— 本群监听总开关"""
    if not ev.is_group:
        return
    if not _is_admin(ev):
        await _ctx.asend_msg(group_id=ev.group_id, message="需要管理员权限")
        return
    s = _get_settings(ev.group_id)
    val = 0 if s['enabled'] else 1
    if _save(ev.group_id, {'enabled': val}):
        await _ctx.asend_msg(group_id=ev.group_id,
                             message=f"群事件监听已{'开启' if val else '关闭'}")


async def cmd_item(ev, match):
    """/watch 撤回 开|关 —— 单项开关"""
    if not ev.is_group:
        return
    if not _is_admin(ev):
        await _ctx.asend_msg(group_id=ev.group_id, message="需要管理员权限")
        return
    text = (ev.raw_message or '').replace('/watch', '', 1).strip()
    name, _, want = text.partition(' ')
    name, want = name.strip(), want.strip()

    reverse = {v: k for k, v in _LABEL.items()}
    canonical = reverse.get(name)
    if not canonical:
        for k, v in _WATCHED.items():
            if name and name in k:
                canonical = k
                break
    if not canonical:
        await _ctx.asend_msg(
            group_id=ev.group_id,
            message="可选：进群 / 退群 / 管理 / 禁言 / 撤回 / 戳一戳")
        return
    val = 0 if want in ('关', '关闭', 'off', '0') else 1
    if _save(ev.group_id, {_WATCHED[canonical]: val}):
        await _ctx.asend_msg(
            group_id=ev.group_id,
            message=f"{_LABEL[canonical]}已{'开启' if val else '关闭'}")


def register(ctx):
    global _ctx
    _ctx = ctx
    _load_cfg(ctx)

    try:
        ctx.create_table(_DDL)
    except Exception as e:      # noqa: BLE001
        logger.error("group_watch 建表失败，退化为纯默认配置运行: %s", e)
    try:
        ctx.db_execute(_MIGRATE)
    except Exception:           # noqa: BLE001
        pass                    # 列已存在或方言不支持，静默

    # 订阅规范通知名：各接入端进来都能用，不必关心协议原始字段名
    for canonical in _WATCHED:
        ctx.on(f'notice.{canonical}', _on_notice)

    ctx.command("/watch", cmd_status, priority=30,
                alias="/群事件,/watchset", description="查看本群事件监听设置")
    ctx.command(r"^/watch\s*开关$", cmd_toggle, priority=30,
                description="开启/关闭本群事件监听")
    ctx.command(r"^/watch\s*\S+\s*(?:开|关|开启|关闭)$", cmd_item, priority=30,
                description="单项开关：/watch 撤回 关")
    ctx.log("群事件监听插件已加载")


def unregister():
    _cache.clear()

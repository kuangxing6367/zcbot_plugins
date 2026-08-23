"""
关键词监控插件 (keyword_watch)
==============================
功能（从 AstrBot「notice」类源插件迁移，功能逻辑重写）：
  超级管理员设置关键词后，任意群消息命中关键词时，自动私聊转发给所有超管。
  /监控 <关键词>   -> 新增监控词
  /取消监控 <关键词> -> 移除
  /监控列表        -> 查看当前监控词
数据：watch_keywords(keyword)。
"""
import re

__plugin_meta__ = {
    "name": "关键词监控",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "群消息命中关键词时私聊转发给超级管理员",
    "priority": 50,
}

ctx = None

_CREATE = """
CREATE TABLE IF NOT EXISTS watch_keywords (
    keyword VARCHAR(128) PRIMARY KEY,
    created_by BIGINT NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL DEFAULT 0
)
"""

_KW_CACHE = set()
_KW_T = 0
_KW_TTL = 60


def _is_super(event) -> bool:
    return getattr(event, "is_superuser", False) or getattr(event, "role", "") == "super"


def _refresh():
    global _KW_T
    import time
    now = time.time()
    if now - _KW_T < _KW_TTL and _KW_CACHE:
        return
    try:
        rows = ctx.db_query("SELECT keyword FROM watch_keywords")
        _KW_CACHE.clear()
        for r in rows:
            _KW_CACHE.add(r["keyword"])
        _KW_T = now
    except Exception as e:
        ctx.logger.warning(f"刷新监控词失败: {e}")


def _superusers():
    try:
        rows = ctx.db_query("SELECT user_id FROM users WHERE role='super'")
        return [int(r["user_id"]) for r in rows]
    except Exception:
        return []


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def handle_add(event, match):
    if not _is_super(event):
        await _reply(event, "需要超级管理员权限")
        return
    kw = (match.group(1) if match else "").strip()
    if not kw:
        await _reply(event, "用法: /监控 <关键词>")
        return
    try:
        ctx.db_execute(
            "INSERT INTO watch_keywords (keyword, created_by, created_at) VALUES (%s,%s,%s)",
            [kw, event.user_id, int(__import__("time").time())],
        )
    except Exception:
        try:
            ctx.db_execute(
                "INSERT OR IGNORE INTO watch_keywords (keyword, created_by, created_at) VALUES (%s,%s,%s)",
                [kw, event.user_id, int(__import__("time").time())],
            )
        except Exception as e:
            await _reply(event, f"添加失败: {e}")
            return
    _KW_CACHE.add(kw)
    await _reply(event, f"已添加监控词「{kw}」")


async def handle_remove(event, match):
    if not _is_super(event):
        await _reply(event, "需要超级管理员权限")
        return
    kw = (match.group(1) if match else "").strip()
    if not kw:
        await _reply(event, "用法: /取消监控 <关键词>")
        return
    try:
        ctx.db_execute("DELETE FROM watch_keywords WHERE keyword=%s", [kw])
    except Exception as e:
        await _reply(event, f"移除失败: {e}")
        return
    _KW_CACHE.discard(kw)
    await _reply(event, f"已移除监控词「{kw}」")


async def handle_list(event, match):
    if not _is_super(event):
        await _reply(event, "需要超级管理员权限")
        return
    _refresh()
    if not _KW_CACHE:
        await _reply(event, "当前没有任何监控词")
        return
    await _reply(event, "🔍 监控词：" + "、".join(sorted(_KW_CACHE)))


async def on_raw(raw: dict, bot_name: str) -> bool:
    if raw.get("post_type") != "message" or raw.get("message_type") != "group":
        return False
    _refresh()
    if not _KW_CACHE:
        return False
    msg = raw.get("message", "")
    if isinstance(msg, list):
        msg = "".join(s.get("data", {}).get("text", "") for s in msg
                      if isinstance(s, dict) and s.get("type") == "text")
    msg = str(msg or "")
    hit = [k for k in _KW_CACHE if k and k in msg]
    if not hit:
        return False
    sups = _superusers()
    if not sups:
        return False
    gid = raw.get("group_id")
    uid = raw.get("user_id")
    text = (f"🔔 关键词命中提醒\n群：{gid}\n用户：{uid}\n"
            f"命中：{', '.join(hit)}\n内容：{msg[:200]}")
    for s in sups:
        try:
            await ctx.asend_msg(user_id=s, group_id=None, message=text, bot=bot_name)
        except Exception:
            pass
    return False


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.db_execute(_CREATE, [])
    ctx.on_raw_message(on_raw)
    ctx.command("^/监控\\s+([\\s\\S]+)", handle_add, priority=50,
                description="新增监控词", require_superuser=True)
    ctx.command("^/取消监控\\s+([\\s\\S]+)", handle_remove, priority=50,
                description="移除监控词", require_superuser=True)
    ctx.command("^/监控列表\\s*$", handle_list, priority=50,
                description="查看监控词", require_superuser=True)
    ctx.logger.info("关键词监控插件注册完成")

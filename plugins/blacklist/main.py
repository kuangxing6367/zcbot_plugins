"""
全局用户黑名单插件 (blacklist)
==============================
功能（从 AstrBot「blacklist」类插件迁移，功能逻辑重写；区别于 qqadmin 的进群黑名单）：
  超级管理员维护一份「全局用户黑名单」，黑名单用户的消息被静默丢弃（机器人不响应）。
  /拉黑 <QQ>          -> 加入黑名单
  /解黑 <QQ>          -> 移出黑名单
  /黑名单             -> 列出当前黑名单
  /黑名单检查 <QQ>    -> 查询某 QQ 是否在黑名单
消息拦截在 on_raw_message 前置阶段完成（返回 True 接管丢弃）。
"""
import re

__plugin_meta__ = {
    "name": "全局用户黑名单",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "超级管理员维护全局用户黑名单，黑名单用户消息被静默丢弃",
    "priority": 5,
}

ctx = None

_CREATE = """
CREATE TABLE IF NOT EXISTS blacklist_global (
    user_id BIGINT PRIMARY KEY,
    reason VARCHAR(255) DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0
)
"""

_BL_CACHE = set()
_BL_CACHE_T = 0
_BL_TTL = 60


def _is_super(event) -> bool:
    return getattr(event, "is_superuser", False) or getattr(event, "role", "") == "super"


def _refresh_cache():
    global _BL_CACHE_T
    import time
    now = time.time()
    if now - _BL_CACHE_T < _BL_TTL and _BL_CACHE:
        return
    try:
        rows = ctx.db_query("SELECT user_id FROM blacklist_global")
        _BL_CACHE.clear()
        for r in rows:
            _BL_CACHE.add(int(r["user_id"]))
        _BL_CACHE_T = now
    except Exception as e:
        ctx.logger.warning(f"刷新黑名单缓存失败: {e}")


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def on_raw(raw: dict, bot_name: str) -> bool:
    """黑名单用户消息静默丢弃"""
    try:
        if raw.get("post_type") != "message":
            return False
        uid = raw.get("user_id")
        if not uid:
            return False
        _refresh_cache()
        if int(uid) in _BL_CACHE:
            ctx.logger.info(f"黑名单用户 {uid} 消息被丢弃")
            return True
        return False
    except Exception as e:
        ctx.logger.warning(f"黑名单拦截异常: {e}")
        return False


async def handle_add(event, match):
    """拉黑 <QQ> [理由]"""
    if not _is_super(event):
        await _reply(event, "需要超级管理员权限")
        return
    text = (match.group(1) if match else "").strip()
    m = re.match(r"(\d{5,})\s*(.*)", text)
    if not m:
        await _reply(event, "用法: /拉黑 <QQ号> [理由]")
        return
    uid = int(m.group(1))
    reason = m.group(2).strip()
    try:
        ctx.db_execute(
            "INSERT INTO blacklist_global (user_id, reason, created_at) VALUES (%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE reason=%s",
            [uid, reason, int(__import__("time").time()), reason],
        )
    except Exception:
        try:
            ctx.db_execute(
                "INSERT OR REPLACE INTO blacklist_global (user_id, reason, created_at) VALUES (%s,%s,%s)",
                [uid, reason, int(__import__("time").time())],
            )
        except Exception as e:
            await _reply(event, f"拉黑失败: {e}")
            return
    _BL_CACHE.add(uid)
    await _reply(event, f"已将 {uid} 加入全局黑名单" + (f"（理由：{reason}）" if reason else ""))


async def handle_remove(event, match):
    """解黑 <QQ>"""
    if not _is_super(event):
        await _reply(event, "需要超级管理员权限")
        return
    text = (match.group(1) if match else "").strip()
    m = re.search(r"(\d{5,})", text)
    if not m:
        await _reply(event, "用法: /解黑 <QQ号>")
        return
    uid = int(m.group(1))
    try:
        ctx.db_execute("DELETE FROM blacklist_global WHERE user_id=%s", [uid])
    except Exception as e:
        await _reply(event, f"解黑失败: {e}")
        return
    _BL_CACHE.discard(uid)
    await _reply(event, f"已将 {uid} 移出全局黑名单")


async def handle_list(event, match):
    """黑名单"""
    if not _is_super(event):
        await _reply(event, "需要超级管理员权限")
        return
    try:
        rows = ctx.db_query("SELECT user_id, reason FROM blacklist_global ORDER BY created_at DESC")
    except Exception as e:
        await _reply(event, f"查询失败: {e}")
        return
    if not rows:
        await _reply(event, "黑名单为空")
        return
    lines = [f"🚫 全局黑名单（{len(rows)} 人）："]
    for r in rows[:50]:
        reason = r.get("reason") or ""
        lines.append(f"  {r['user_id']}" + (f" — {reason}" if reason else ""))
    await _reply(event, "\n".join(lines))


async def handle_check(event, match):
    """黑名单检查 <QQ>"""
    text = (match.group(1) if match else "").strip()
    m = re.search(r"(\d{5,})", text)
    if not m:
        await _reply(event, "用法: /黑名单检查 <QQ号>")
        return
    uid = int(m.group(1))
    _refresh_cache()
    await _reply(event, f"{uid} {'在' if uid in _BL_CACHE else '不在'}全局黑名单中")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.db_execute(_CREATE, [])
    ctx.on_raw_message(on_raw)
    ctx.command("^/拉黑\\s+([\\s\\S]+)", handle_add, priority=50,
                description="加入全局黑名单", require_superuser=True)
    ctx.command("^/解黑\\s+([\\s\\S]+)", handle_remove, priority=50,
                description="移出全局黑名单", require_superuser=True)
    ctx.command("^/黑名单\\s*$", handle_list, priority=50,
                description="列出黑名单", require_superuser=True)
    ctx.command("^/黑名单检查\\s+([\\s\\S]+)", handle_check, priority=50,
                description="查询是否在黑名单", require_superuser=True)
    ctx.logger.info("全局黑名单插件注册完成")

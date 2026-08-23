"""
用户消息日志插件 (user_logger)
==============================
功能（从 AstrBot「user_logger」类插件迁移，功能逻辑重写）：
  - 记录所有入站消息到插件表 user_log（群/私聊、用户、纯文本、时间戳）
  - /我的发言数     -> 查询自己累计发言条数
  - /群发言排行     -> 当前群发言量 Top10
  - /发言统计 [群号]-> 群总发言数/人数
数据持久化：user_log(id, group_id, user_id, content, created_at)。
"""
import re

__plugin_meta__ = {
    "name": "用户消息日志",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "记录所有入站消息，提供发言数/排行/统计查询",
    "priority": 50,
}

ctx = None

_CREATE = """
CREATE TABLE IF NOT EXISTS user_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id BIGINT NOT NULL DEFAULT 0,
    user_id BIGINT NOT NULL,
    content TEXT,
    created_at INTEGER NOT NULL DEFAULT 0
)
"""


def _text_of(raw):
    msg = raw.get("message", "")
    if isinstance(msg, list):
        return "".join(s.get("data", {}).get("text", "") for s in msg
                        if isinstance(s, dict) and s.get("type") == "text")
    return str(msg or "")


_async_log = []


def on_raw(raw: dict, bot_name: str) -> bool:
    """记录消息（不接管，返回 False）"""
    try:
        if raw.get("post_type") != "message":
            return False
        uid = raw.get("user_id")
        gid = raw.get("group_id") or 0
        if not uid:
            return False
        content = _text_of(raw)[:2000]
        ctx.db_execute(
            "INSERT INTO user_log (group_id, user_id, content, created_at) VALUES (%s,%s,%s,%s)",
            [gid, uid, content, int(__import__("time").time())],
        )
    except Exception:
        pass
    return False


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def handle_mine(event, match):
    """我的发言数"""
    try:
        rows = ctx.db_query(
            "SELECT COUNT(*) AS c FROM user_log WHERE user_id=%s", [event.user_id]
        )
        c = rows[0]["c"] if rows else 0
    except Exception as e:
        await _reply(event, f"查询失败: {e}")
        return
    await _reply(event, f"你累计发言 {c} 条（已被本插件记录）")


async def handle_rank(event, match):
    """群发言排行"""
    if not getattr(event, "is_group", False):
        await _reply(event, "请在群聊中使用")
        return
    try:
        rows = ctx.db_query(
            "SELECT user_id, COUNT(*) AS c FROM user_log WHERE group_id=%s GROUP BY user_id ORDER BY c DESC LIMIT 10",
            [event.group_id],
        )
    except Exception as e:
        await _reply(event, f"查询失败: {e}")
        return
    if not rows:
        await _reply(event, "暂无发言记录")
        return

    def nick(uid):
        try:
            info = ctx.get_member_info(event.group_id, uid) or {}
            return info.get("card") or info.get("nickname") or str(uid)
        except Exception:
            return str(uid)

    lines = ["🏆 群发言排行 Top10："]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {nick(int(r['user_id']))} — {r['c']} 条")
    await _reply(event, "\n".join(lines))


async def handle_stat(event, match):
    """发言统计 [群号]"""
    text = (match.group(1) if match else "").strip()
    m = re.search(r"(\d{5,})", text)
    gid = int(m.group(1)) if m else (event.group_id or 0)
    try:
        rows = ctx.db_query(
            "SELECT COUNT(*) AS c, COUNT(DISTINCT user_id) AS u FROM user_log WHERE group_id=%s",
            [gid],
        )
        c, u = (rows[0]["c"], rows[0]["u"]) if rows else (0, 0)
    except Exception as e:
        await _reply(event, f"查询失败: {e}")
        return
    await _reply(event, f"群 {gid}：累计 {c} 条消息，{u} 位用户发言")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.db_execute(_CREATE, [])
    ctx.on_raw_message(on_raw)
    ctx.command("^/我的发言数\\s*$", handle_mine, priority=50, description="查询自己发言数")
    ctx.command("^/群发言排行\\s*$", handle_rank, priority=50, description="群发言排行")
    ctx.command("^/发言统计\\s*([\\s\\S]*)$", handle_stat, priority=50, description="发言统计")
    ctx.logger.info("用户消息日志插件注册完成")

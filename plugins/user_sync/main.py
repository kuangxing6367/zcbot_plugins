"""
用户同步插件 (user_sync)
=========================
功能（从 AstrBot「user_sync」类插件迁移，功能逻辑重写）：
  /同步群成员 [群号]  -> 拉取群成员列表并写入插件索引表 user_index
  /查用户 <QQ>        -> 查询该用户出现在哪些已同步的群
  /同步状态            -> 已同步群数量与用户总量
数据：user_index(group_id, user_id, nickname, role)。
"""
import re

__plugin_meta__ = {
    "name": "用户同步",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "拉取并索引群成员，支持按 QQ 反查所在群",
    "priority": 50,
}

ctx = None

_CREATE = """
CREATE TABLE IF NOT EXISTS user_index (
    group_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    nickname VARCHAR(128) DEFAULT '',
    role VARCHAR(16) DEFAULT 'member',
    PRIMARY KEY (group_id, user_id)
)
"""


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


def _nick(m):
    return m.get("card") or m.get("nickname") or str(m.get("user_id", ""))


async def handle_sync(event, match):
    """同步群成员 [群号]"""
    if not (getattr(event, "is_superuser", False) or getattr(event, "role", "") in ("admin", "owner", "super")):
        await _reply(event, "需要管理员权限")
        return
    text = (match.group(1) if match else "").strip()
    m = re.search(r"(\d{5,})", text)
    gid = int(m.group(1)) if m else (event.group_id if getattr(event, "is_group", False) else None)
    if not gid:
        await _reply(event, "用法: /同步群成员 [群号]")
        return
    try:
        members = ctx.get_member_list(gid) or []
    except Exception as e:
        await _reply(event, f"获取成员失败: {e}")
        return
    cnt = 0
    for mem in members:
        uid = mem.get("user_id")
        if not uid:
            continue
        try:
            ctx.db_execute(
                "INSERT INTO user_index (group_id, user_id, nickname, role) VALUES (%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE nickname=%s, role=%s",
                [gid, uid, _nick(mem), mem.get("role", "member"), _nick(mem), mem.get("role", "member")],
            )
        except Exception:
            try:
                ctx.db_execute(
                    "INSERT OR REPLACE INTO user_index (group_id, user_id, nickname, role) VALUES (%s,%s,%s,%s)",
                    [gid, uid, _nick(mem), mem.get("role", "member")],
                )
            except Exception:
                continue
        cnt += 1
    await _reply(event, f"已同步群 {gid} 成员 {cnt} 人")


async def handle_find(event, match):
    """查用户 <QQ>"""
    text = (match.group(1) if match else "").strip()
    m = re.search(r"(\d{5,})", text)
    if not m:
        await _reply(event, "用法: /查用户 <QQ号>")
        return
    uid = int(m.group(1))
    try:
        rows = ctx.db_query(
            "SELECT group_id, nickname, role FROM user_index WHERE user_id=%s", [uid]
        )
    except Exception as e:
        await _reply(event, f"查询失败: {e}")
        return
    if not rows:
        await _reply(event, f"索引中未找到用户 {uid}")
        return
    lines = [f"🔍 {uid} 出现在 {len(rows)} 个群："]
    for r in rows:
        lines.append(f"  群 {r['group_id']}（{r['nickname']}/{r['role']}）")
    await _reply(event, "\n".join(lines))


async def handle_status(event, match):
    try:
        g = ctx.db_query("SELECT COUNT(DISTINCT group_id) AS c FROM user_index")
        u = ctx.db_query("SELECT COUNT(DISTINCT user_id) AS c FROM user_index")
        gc = g[0]["c"] if g else 0
        uc = u[0]["c"] if u else 0
    except Exception as e:
        await _reply(event, f"查询失败: {e}")
        return
    await _reply(event, f"已同步 {gc} 个群，索引 {uc} 名用户")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.db_execute(_CREATE, [])
    ctx.command("^/同步群成员\\s*([\\s\\S]*)$", handle_sync, priority=50, description="同步群成员", require_admin=True)
    ctx.command("^/查用户\\s+([\\s\\S]+)", handle_find, priority=50, description="反查用户所在群")
    ctx.command("^/同步状态\\s*$", handle_status, priority=50, description="同步状态")
    ctx.logger.info("用户同步插件注册完成")

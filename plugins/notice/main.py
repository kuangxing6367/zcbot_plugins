"""
群通知 / 广播插件 (notice)
==========================
功能（从 AstrBot「notice」类插件迁移，功能逻辑重写）：
  /群广播 <内容>        -> 超级管理员向所有已加入的群发送同一条通知
  /群通知 <群号> <内容>  -> 向指定群发送通知
  /我的群               -> 列出机器人所在群（群号+名称）
数据来源：OneBot get_group_list。
"""
import re

__plugin_meta__ = {
    "name": "群通知广播",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "超级管理员向全部或指定群发送通知/广播",
    "priority": 50,
}

ctx = None


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


def _my_groups():
    try:
        resp = ctx.api("get_group_list")
        if isinstance(resp, dict):
            data = resp.get("data") or resp
            if isinstance(data, list):
                return data
        if isinstance(resp, list):
            return resp
    except Exception as e:
        ctx.logger.warning(f"获取群列表失败: {e}")
    return []


async def handle_broadcast(event, match):
    """群广播 <内容>"""
    if not getattr(event, "is_superuser", False):
        await _reply(event, "需要超级管理员权限")
        return
    content = (match.group(1) if match else "").strip()
    if not content:
        await _reply(event, "用法: /群广播 <要发送的内容>")
        return
    groups = _my_groups()
    if not groups:
        await _reply(event, "未获取到任何群列表")
        return
    ok = 0
    for g in groups:
        gid = g.get("group_id")
        if not gid:
            continue
        try:
            await ctx.aapi("send_group_msg", group_id=gid, message=content)
            ok += 1
        except Exception as e:
            ctx.logger.warning(f"广播到群 {gid} 失败: {e}")
    await _reply(event, f"📢 广播已发送至 {ok}/{len(groups)} 个群")


async def handle_notify(event, match):
    """群通知 <群号> <内容>"""
    if not (getattr(event, "is_superuser", False) or getattr(event, "role", "") in ("admin", "owner", "super")):
        await _reply(event, "需要管理员权限")
        return
    text = (match.group(1) if match else "").strip()
    m = re.match(r"(\d{5,})\s+(.+)", text, re.S)
    if not m:
        await _reply(event, "用法: /群通知 <群号> <内容>")
        return
    gid = int(m.group(1))
    content = m.group(2).strip()
    try:
        await ctx.aapi("send_group_msg", group_id=gid, message=content)
    except Exception as e:
        await _reply(event, f"发送失败: {e}")
        return
    await _reply(event, f"已向群 {gid} 发送通知")


async def handle_my_groups(event, match):
    """我的群"""
    if not getattr(event, "is_superuser", False):
        await _reply(event, "需要超级管理员权限")
        return
    groups = _my_groups()
    if not groups:
        await _reply(event, "未获取到群列表")
        return
    lines = [f"📋 机器人所在群（{len(groups)} 个）："]
    for g in groups[:50]:
        lines.append(f"  {g.get('group_name','?')} ({g.get('group_id')})")
    await _reply(event, "\n".join(lines))


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/群广播\\s+([\\s\\S]+)", handle_broadcast, priority=50,
                description="向所有群广播通知", require_superuser=True)
    ctx.command("/群通知\\s+([\\s\\S]+)", handle_notify, priority=50,
                description="向指定群发送通知", require_admin=True)
    ctx.command("/我的群\\s*$", handle_my_groups, priority=50,
                description="列出机器人所在群", require_superuser=True)
    ctx.logger.info("群通知插件注册完成")

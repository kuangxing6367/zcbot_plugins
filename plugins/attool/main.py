"""
@ 工具插件 (attool)
====================
  /atall <内容>        -> 发送「@全体成员」+ 内容（需群管权限）
  /at <QQ...> <内容>   -> 同时 @ 多个指定成员并附内容
  /at帮助              -> 说明
使用 OneBot at 消息段（CQ: [CQ:at,qq=all] / [CQ:at,qq=<id>]）。
"""
import re

__plugin_meta__ = {
    "name": "@工具",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "@全体成员 / @多人工具，群管消息触达",
    "priority": 50,
}

ctx = None


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def handle_atall(event, match):
    """atall <内容>"""
    if not getattr(event, "is_group", False):
        await _reply(event, "请在群聊中使用")
        return
    content = (match.group(1) if match else "").strip()
    msg = [{"type": "at", "data": {"qq": "all"}}, (content or "")]
    await ctx.asend_msg(group_id=event.group_id, message=msg)
    # 群管权限提示
    await _reply(event, "已发送 @全体成员" + (f"：{content}" if content else ""))


async def handle_at(event, match):
    """at <QQ...> <内容>"""
    if not getattr(event, "is_group", False):
        await _reply(event, "请在群聊中使用")
        return
    text = (match.group(1) if match else "").strip()
    parts = text.split()
    if len(parts) < 2:
        await _reply(event, "用法: /at <QQ1> [QQ2...] <内容>")
        return
    # 末段为内容，其余为 QQ
    content = parts[-1]
    qqs = [p for p in parts[:-1] if p.isdigit()]
    if not qqs:
        await _reply(event, "未识别到有效的 QQ 号")
        return
    segs = []
    for q in qqs:
        segs.append({"type": "at", "data": {"qq": q}})
    segs.append(content)
    await ctx.asend_msg(group_id=event.group_id, message=segs)


async def handle_help(event, match):
    await _reply(event,
        "📣 @工具\n"
        "▸ /atall <内容>  → @全体成员\n"
        "▸ /at <QQ1> [QQ2...] <内容>  → @多个指定成员\n"
        "▸ /at帮助")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/atall\\s*([\\s\\S]*)", handle_atall, priority=50,
                description="@全体成员", require_admin=True)
    ctx.command("/at\\s+([\\s\\S]+)", handle_at, priority=50,
                description="@多个成员", require_admin=True)
    ctx.command("/at帮助\\s*$", handle_help, priority=50, description="@工具帮助")
    ctx.logger.info("@工具插件注册完成")

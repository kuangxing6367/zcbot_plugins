"""
加群邀请插件 (addgroup)
========================
  /加群链接 [群号]  -> 为指定群（默认当前群）生成邀请链接并发送
  /加群 [群号]      -> 同上（别名）
  /退群 <群号>      -> 机器人主动退出某群（需超管）
数据来源：OneBot create_group_invite_link / set_group_leave。
"""
import re

__plugin_meta__ = {
    "name": "加群邀请",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "生成群邀请链接、机器人主动退群",
    "priority": 50,
}

ctx = None


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def handle_link(event, match):
    """加群链接 [群号]"""
    text = (match.group(1) if match else "").strip()
    m = re.search(r"(\d{5,})", text)
    gid = int(m.group(1)) if m else (event.group_id if getattr(event, "is_group", False) else None)
    if not gid:
        await _reply(event, "用法: /加群链接 [群号]")
        return
    try:
        resp = await ctx.aapi("create_group_invite_link", group_id=gid)
        link = ""
        if isinstance(resp, dict):
            link = resp.get("data", {}).get("link") or resp.get("link") or ""
        if not link:
            await _reply(event, "生成链接失败（接口未返回链接，可能无权限）")
            return
        await _reply(event, f"🔗 群 {gid} 邀请链接：\n{link}")
    except Exception as e:
        await _reply(event, f"生成失败: {e}")


async def handle_leave(event, match):
    """退群 <群号>"""
    if not getattr(event, "is_superuser", False):
        await _reply(event, "需要超级管理员权限")
        return
    text = (match.group(1) if match else "").strip()
    m = re.search(r"(\d{5,})", text)
    if not m:
        await _reply(event, "用法: /退群 <群号>")
        return
    gid = int(m.group(1))
    try:
        await ctx.aapi("set_group_leave", group_id=gid, is_dismiss=False)
        await _reply(event, f"机器人已退出群 {gid}")
    except Exception as e:
        await _reply(event, f"退群失败: {e}")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("^/加群链接\\s*([\\s\\S]*)", handle_link, priority=50, description="生成群邀请链接")
    ctx.command("^/加群\\s*([\\s\\S]*)", handle_link, priority=50, description="生成群邀请链接（别名）")
    ctx.command("^/退群\\s*([\\s\\S]+)", handle_leave, priority=50,
                description="机器人退群", require_superuser=True)
    ctx.logger.info("加群邀请插件注册完成")

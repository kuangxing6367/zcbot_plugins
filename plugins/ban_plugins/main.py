"""
插件开关管理插件 (ban_plugins)
==============================
  超级管理员可对任意插件做启停管理：
  /插件列表                 -> 列出已加载插件及其当前状态
  /禁用插件 <名> [群号]     -> 在指定群（或全局留空=本群）禁用某插件
  /启用插件 <名> [群号]     -> 启用某插件
  /插件状态 [群号]          -> 查看某群各插件启停状态
  /插件帮助                 -> 说明
群级开关走框架 API：ctx.disable_plugin_in_group / enable_plugin_in_group /
is_plugin_enabled_in_group / get_plugin_status_list。
"""
import re

__plugin_meta__ = {
    "name": "插件开关管理",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "超级管理员对任意插件做群级/全局启停管理",
    "priority": 50,
}

ctx = None


def _is_super(event) -> bool:
    return getattr(event, "is_superuser", False) or getattr(event, "role", "") == "super"


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def handle_list(event, match):
    """插件列表"""
    if not _is_super(event):
        await _reply(event, "需要超级管理员权限")
        return
    try:
        status = ctx.get_plugin_status_list(event.group_id or 0)
    except Exception as e:
        await _reply(event, f"获取列表失败: {e}")
        return
    lines = ["📦 已加载插件："]
    for name, enabled in status.items():
        lines.append(f"  [{'✅' if enabled else '❌'}] {name}")
    await _reply(event, "\n".join(lines))


async def handle_status(event, match):
    """插件状态 [群号]"""
    if not _is_super(event):
        await _reply(event, "需要超级管理员权限")
        return
    gid = event.group_id or 0
    m = re.search(r"\d{5,}", (match.group(1) if match else "") or "")
    if m:
        gid = int(m.group(0))
    try:
        status = ctx.get_plugin_status_list(gid)
    except Exception as e:
        await _reply(event, f"获取状态失败: {e}")
        return
    lines = [f"📊 群 {gid} 插件状态："]
    for name, enabled in status.items():
        lines.append(f"  [{'✅' if enabled else '❌'}] {name}")
    await _reply(event, "\n".join(lines))


async def handle_toggle(event, match):
    """禁用/启用插件 <名> [群号]"""
    if not _is_super(event):
        await _reply(event, "需要超级管理员权限")
        return
    text = (match.group(2) or "").strip()
    enable = (match.group(1) == "启用") if match else ("启用" in (event.message or ""))
    parts = text.split()
    if not parts:
        await _reply(event, "用法: /禁用插件 <插件名> [群号]")
        return
    name = parts[0]
    gid = event.group_id
    gm = re.search(r"\d{5,}", text)
    if gm:
        gid = int(gm.group(0))
    try:
        if enable:
            ctx.enable_plugin_in_group(name, gid)
        else:
            ctx.disable_plugin_in_group(name, gid)
    except Exception as e:
        await _reply(event, f"操作失败: {e}")
        return
    scope = f"群 {gid}" if gid else "全局"
    await _reply(event, f"已{'启用' if enable else '禁用'}插件 {name}（{scope}）")


async def handle_help(event, match):
    await _reply(event,
        "🔧 插件开关管理（超管）\n"
        "▸ /插件列表\n"
        "▸ /插件状态 [群号]\n"
        "▸ /禁用插件 <名> [群号]\n"
        "▸ /启用插件 <名> [群号]")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/插件列表\\s*$", handle_list, priority=50,
                description="列出已加载插件", require_superuser=True)
    ctx.command("/插件状态\\s*(.*)", handle_status, priority=50,
                description="查看插件状态", require_superuser=True)
    ctx.command("^/(禁用|启用)插件\\s+(.+)$", handle_toggle, priority=50,
                description="启停插件", require_superuser=True)
    ctx.command("/插件帮助\\s*$", handle_help, priority=50, description="插件管理帮助")
    ctx.logger.info("插件开关管理注册完成")

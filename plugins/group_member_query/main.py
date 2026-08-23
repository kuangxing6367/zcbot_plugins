"""
群成员查询插件 (group_member_query)
===================================
功能（从 AstrBot「群成员查询」类插件迁移，功能逻辑重写）：
  /查成员 <昵称片段>  -> 在当前群按昵称模糊搜索成员（返回前 10 条）
  /成员 <QQ号>        -> 查询某成员在本群的资料（昵称/性别/等级/加群时间）
  /群成员数           -> 当前群成员总数
数据来源：ctx.get_member_list / get_member_info。
"""
import re

__plugin_meta__ = {
    "name": "群成员查询",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "按昵称/QQ 模糊查询群成员资料",
    "priority": 50,
}

ctx = None


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


def _card(m):
    return m.get("card") or m.get("nickname") or str(m.get("user_id", ""))


async def handle_search(event, match):
    """查成员 <昵称片段>"""
    if not getattr(event, "is_group", False):
        await _reply(event, "请在群聊中使用")
        return
    kw = (match.group(1) if match else "").strip()
    if not kw:
        await _reply(event, "用法: /查成员 <昵称片段>")
        return
    try:
        members = ctx.get_member_list(event.group_id) or []
    except Exception as e:
        await _reply(event, f"获取成员列表失败: {e}")
        return
    hits = [m for m in members if kw.lower() in _card(m).lower()][:10]
    if not hits:
        await _reply(event, f"未找到昵称包含「{kw}」的成员")
        return
    lines = [f"🔍 匹配「{kw}」（{len(hits)} 条）："]
    for m in hits:
        lines.append(f"  {_card(m)} ({m.get('user_id')})")
    await _reply(event, "\n".join(lines))


async def handle_info(event, match):
    """成员 <QQ>"""
    if not getattr(event, "is_group", False):
        await _reply(event, "请在群聊中使用")
        return
    text = (match.group(1) if match else "").strip()
    m = re.search(r"(\d{5,})", text)
    if not m:
        await _reply(event, "用法: /成员 <QQ号>")
        return
    uid = int(m.group(1))
    try:
        info = ctx.get_member_info(event.group_id, uid)
    except Exception as e:
        await _reply(event, f"查询失败: {e}")
        return
    if not info:
        await _reply(event, f"未找到成员 {uid}")
        return
    role_map = {"owner": "群主", "admin": "管理员", "member": "成员"}
    lines = [
        f"👤 成员资料：",
        f"昵称：{_card(info)}",
        f"QQ：{info.get('user_id')}",
        f"群名片：{info.get('card') or '无'}",
        f"身份：{role_map.get(info.get('role'), info.get('role'))}",
        f"群等级：{info.get('level', '?')}",
        f"加群时间：{info.get('join_time', '?')}",
    ]
    await _reply(event, "\n".join(lines))


async def handle_count(event, match):
    """群成员数"""
    if not getattr(event, "is_group", False):
        await _reply(event, "请在群聊中使用")
        return
    try:
        members = ctx.get_member_list(event.group_id) or []
    except Exception as e:
        await _reply(event, f"获取失败: {e}")
        return
    await _reply(event, f"本群当前共有 {len(members)} 名成员")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("^/查成员\\s+([\\s\\S]+)", handle_search, priority=50, description="按昵称搜索群成员")
    ctx.command("^/成员\\s+([\\s\\S]+)", handle_info, priority=50, description="查询成员资料")
    ctx.command("^/群成员数\\s*$", handle_count, priority=50, description="群成员总数")
    ctx.logger.info("群成员查询插件注册完成")

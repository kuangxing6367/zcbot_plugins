"""
远程管理插件 (remote_admin)
============================
  超级管理员通过「私聊机器人」下发管理指令，实现远程控制：
  /远程 禁言 <群号> <QQ> <秒>
  /远程 踢 <群号> <QQ>
  /远程 全体禁言 <群号> <on|off>
  /远程 广播 <文本>        -> 向所有群广播
  /远程 状态               -> 框架运行状态摘要
仅超管私聊生效；群聊与非超管一律忽略。
"""
import re

__plugin_meta__ = {
    "name": "远程管理",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "超级管理员私聊下发指令远程管理群/广播/查状态",
    "priority": 50,
}

ctx = None


async def _reply(event, msg):
    # 远程管理仅私聊回复
    await ctx.asend_msg(user_id=event.user_id, group_id=None, message=msg)


async def handle_remote(event, match):
    """远程 <子命令...>"""
    if not getattr(event, "is_superuser", False):
        return  # 静默忽略非超管
    if getattr(event, "is_group", False):
        return  # 仅私聊
    text = (match.group(1) if match else "").strip()
    if not text:
        await _reply(event, "用法: /远程 <禁言|踢|全体禁言|广播|状态> ...")
        return
    try:
        if text.startswith("禁言"):
            m = re.search(r"禁言\s+(\d+)\s+(\d+)\s+(\d+)", text)
            if not m:
                await _reply(event, "用法: /远程 禁言 <群号> <QQ> <秒>")
                return
            gid, uid, sec = int(m.group(1)), int(m.group(2)), int(m.group(3))
            await ctx.aapi("set_group_ban", group_id=gid, user_id=uid, duration=sec)
            await _reply(event, f"已禁言 {uid}（群 {gid}）{sec} 秒")

        elif text.startswith("踢"):
            m = re.search(r"踢\s+(\d+)\s+(\d+)", text)
            if not m:
                await _reply(event, "用法: /远程 踢 <群号> <QQ>")
                return
            gid, uid = int(m.group(1)), int(m.group(2))
            await ctx.aapi("set_group_kick", group_id=gid, user_id=uid)
            await _reply(event, f"已踢出 {uid}（群 {gid}）")

        elif text.startswith("全体禁言"):
            m = re.search(r"全体禁言\s+(\d+)\s+(on|off)", text)
            if not m:
                await _reply(event, "用法: /远程 全体禁言 <群号> <on|off>")
                return
            gid, on = int(m.group(1)), m.group(2) == "on"
            await ctx.aapi("set_group_whole_ban", group_id=gid, enable=on)
            await _reply(event, f"群 {gid} 全体禁言已{'开启' if on else '关闭'}")

        elif text.startswith("广播"):
            content = text[len("广播"):].strip()
            if not content:
                await _reply(event, "用法: /远程 广播 <文本>")
                return
            groups = []
            try:
                resp = ctx.api("get_group_list")
                if isinstance(resp, dict):
                    data = resp.get("data") or resp
                    if isinstance(data, list):
                        groups = data
            except Exception:
                pass
            ok = 0
            for g in groups:
                gid = g.get("group_id")
                if not gid:
                    continue
                try:
                    await ctx.aapi("send_group_msg", group_id=gid, message=content)
                    ok += 1
                except Exception:
                    pass
            await _reply(event, f"广播已发至 {ok} 个群")

        elif text.startswith("状态"):
            try:
                rows = ctx.db_query("SELECT COUNT(*) AS c FROM groups")
                gc = rows[0]["c"] if rows else 0
                rows = ctx.db_query("SELECT COUNT(*) AS c FROM users")
                uc = rows[0]["c"] if rows else 0
            except Exception:
                gc = uc = "?"
            await _reply(event, f"📊 框架状态：已记录群 {gc} 个，用户 {uc} 人")

        else:
            await _reply(event, "未知子命令。支持：禁言 / 踢 / 全体禁言 / 广播 / 状态")
    except Exception as e:
        await _reply(event, f"执行失败: {e}")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/远程\\s*([\\s\\S]+)", handle_remote, priority=50,
                description="远程管理指令（超管私聊）", require_superuser=True)
    ctx.logger.info("远程管理插件注册完成")

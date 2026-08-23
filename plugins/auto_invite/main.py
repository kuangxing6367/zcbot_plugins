"""
自动进群 / 邀请处理插件 (auto_invite)
=====================================
功能（从 AstrBot「auto_invite」类插件迁移，功能逻辑重写）：
  - 自动接受「别人邀请机器人进群」的请求（auto_accept_invite，默认开）
  - 可选自动通过「用户申请加群」请求（auto_approve_join，默认关）
  - /自动进群 开/关  -> 切换自动接受邀请
  - /自动通过 开/关  -> 切换自动通过加群申请
事件来源：OneBot request(group)。用 on_raw_message 直接识别最稳健。
"""
import re

__plugin_meta__ = {
    "name": "自动进群",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "自动接受邀请机器人进群，可选自动通过加群申请",
    "priority": 50,
}

ctx = None

_CFG_TTL = 120
_cache = {"t": 0, "data": None}


def _cfg(key, default):
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


def _load_bool(key, default):
    v = _cfg(key, default)
    return str(v).lower() in ("1", "true", "yes", "on") if v is not None else default


def _is_group_admin(event) -> bool:
    return getattr(event, "is_superuser", False) or getattr(event, "role", "") in ("admin", "owner", "super")


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def on_raw(raw: dict, bot_name: str) -> bool:
    """识别加群/邀请请求并自动处理"""
    try:
        if raw.get("post_type") != "request":
            return False
        if raw.get("request_type") != "group":
            return False
        flag = raw.get("flag")
        sub_type = raw.get("sub_type") or raw.get("type")  # 'invite' / 'add'
        if not flag:
            return False
        if sub_type == "invite" and _load_bool("auto_accept_invite", True):
            await ctx.aapi("set_group_add_request", flag=flag, sub_type="invite",
                           approve=True, bot=bot_name)
            ctx.logger.info(f"自动接受进群邀请: {raw.get('group_id')}")
            return True
        if sub_type == "add" and _load_bool("auto_approve_join", False):
            await ctx.aapi("set_group_add_request", flag=flag, sub_type="add",
                           approve=True, bot=bot_name)
            ctx.logger.info(f"自动通过加群申请: {raw.get('user_id')}")
            return True
        return False
    except Exception as e:
        ctx.logger.warning(f"自动进群处理异常: {e}")
        return False


async def handle_toggle(event, match):
    """自动进群/自动通过 开/关"""
    if not _is_group_admin(event):
        await _reply(event, "需要管理员权限")
        return
    on = "开" in (event.message or "")
    key = "auto_approve_join" if "通过" in (event.message or "") else "auto_accept_invite"
    # 写入插件配置（持久化）
    try:
        ctx.db_execute(
            "INSERT INTO plugin_configs (plugin_name, config_key, config_value) "
            "VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE config_value=%s",
            ["auto_invite", key, "1" if on else "0", "1" if on else "0"],
        )
    except Exception:
        try:
            ctx.db_execute(
                "INSERT OR REPLACE INTO plugin_configs (plugin_name, config_key, config_value) "
                "VALUES (%s,%s,%s)", ["auto_invite", key, "1" if on else "0"],
            )
        except Exception as e:
            await _reply(event, f"设置失败: {e}")
            return
    label = "自动通过加群申请" if key == "auto_approve_join" else "自动接受进群邀请"
    await _reply(event, f"{label}已{'开启' if on else '关闭'}")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.on_raw_message(on_raw)
    ctx.command("^自动进群\\s+(开|关)\\s*$", handle_toggle, priority=50,
                description="切换自动接受进群邀请", require_admin=True)
    ctx.command("^自动通过\\s+(开|关)\\s*$", handle_toggle, priority=50,
                description="切换自动通过加群申请", require_admin=True)
    ctx.logger.info("自动进群插件注册完成")

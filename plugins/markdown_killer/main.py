"""
去格式 / Markdown 清理插件 (markdown_killer)
===========================================
  /去md <文本>      -> 去除常见 Markdown 语法后原样返回纯文本
  去格式 开 / 关     -> 群内开关「自动模式」：当群消息疑似 Markdown 时，机器人回复其纯文本版
  /去md帮助          -> 使用说明
去除对象：标题 #、加粗 **、斜体 *、行内码 `、代码块 ```、链接 [x](y)、
列表 -/*、引用 >、分割线 ---、粗体 __ 等。保留原文语义文字。
"""
import re

__plugin_meta__ = {
    "name": "去格式",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "去除消息中的 Markdown 语法，提供纯文本；支持群内自动模式",
    "priority": 50,
}

ctx = None

_MD_PATTERNS = [
    (r"```[\s\S]*?```", ""),          # 代码块
    (r"`([^`]+)`", r"\1"),            # 行内代码
    (r"\*\*(.+?)\*\*", r"\1"),        # 加粗 **
    (r"__(.+?)__", r"\1"),            # 加粗 __
    (r"\*(.+?)\*", r"\1"),            # 斜体 *
    (r"_(.+?)_", r"\1"),              # 斜体 _
    (r"!?\[([^\]]*)\]\([^)]*\)", r"\1"),  # 链接/图片
    (r"^#{1,6}\s*", "", re.M),        # 标题
    (r"^\s*[-*+]\s+", "", re.M),      # 无序列表
    (r"^\s*\d+\.\s+", "", re.M),      # 有序列表
    (r"^\s*>\s?", "", re.M),          # 引用
    (r"^---+$", "", re.M),            # 分割线
    (r"^\s*\|.+\|\s*$", "", re.M),    # 表格行
]


def _strip(md: str) -> str:
    text = md or ""
    for pat, rep in _MD_PATTERNS:
        text = re.sub(pat, rep, text)
    # 折叠多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _looks_like_md(text: str) -> bool:
    if not text:
        return False
    markers = ["```", "**", "~~", ">#", ">-", "|", "![", "[", "](http"]
    score = sum(1 for m in markers if m in text)
    return score >= 2 and len(text) > 20


def _cfg(key, default):
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


def _load_bool(key, default):
    v = _cfg(key, default)
    return str(v).lower() in ("1", "true", "yes", "on") if v is not None else default


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def handle_strip(event, match):
    """去md <文本>"""
    text = (match.group(1) if match else "") or (event.message or "")
    text = text.strip()
    if not text:
        await _reply(event, "用法: /去md <要清理的 Markdown 文本>")
        return
    await _reply(event, _strip(text))


async def handle_help(event, match):
    await _reply(event,
        "🧹 去格式插件\n"
        "▸ /去md <文本>  → 去除 Markdown 后返回纯文本\n"
        "▸ 去格式 开/关   → 群内自动模式（疑似 Markdown 的消息自动回复纯文本版）\n"
        "▸ /去md帮助      → 本说明")


async def handle_toggle(event, match):
    """去格式 开/关（群内，需管理员）"""
    if not getattr(event, "is_group", False):
        await _reply(event, "请在群聊中使用")
        return
    if not (getattr(event, "role", "") in ("admin", "owner", "super")):
        await _reply(event, "需要管理员权限")
        return
    on = "开" in (event.message or "")
    key = f"mdkiller_auto_{event.group_id}"
    # 用插件配置表持久化群开关
    try:
        ctx.db_execute(
            "INSERT INTO plugin_configs (plugin_name, config_key, config_value) "
            "VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE config_value=%s",
            ["markdown_killer", key, "1" if on else "0", "1" if on else "0"],
        )
    except Exception:
        # SQLite 无 ON DUPLICATE，回退 upsert
        try:
            ctx.db_execute(
                "INSERT OR REPLACE INTO plugin_configs (plugin_name, config_key, config_value) "
                "VALUES (%s,%s,%s)", ["markdown_killer", key, "1" if on else "0"],
            )
        except Exception as e:
            await _reply(event, f"设置失败: {e}")
            return
    await _reply(event, f"自动去格式已{'开启' if on else '关闭'}")


async def on_raw(raw: dict, bot_name: str) -> bool:
    """自动模式：群消息疑似 Markdown 时回复纯文本版"""
    try:
        if raw.get("post_type") != "message":
            return False
        if raw.get("message_type") != "group":
            return False
        gid = raw.get("group_id")
        if not gid:
            return False
        key = f"mdkiller_auto_{gid}"
        row = ctx.db_query_one(
            "SELECT config_value FROM plugin_configs WHERE plugin_name=%s AND config_key=%s",
            ["markdown_killer", key],
        )
        if not row or str(row.get("config_value")) != "1":
            return False
        msg = raw.get("message", "")
        if isinstance(msg, list):
            msg = "".join(s.get("data", {}).get("text", "") for s in msg
                          if isinstance(s, dict) and s.get("type") == "text")
        msg = str(msg or "")
        if not _looks_like_md(msg):
            return False
        plain = _strip(msg)
        if not plain:
            return False
        await ctx.asend_msg(
            user_id=raw.get("user_id"), group_id=gid, message=plain, bot=bot_name,
        )
        return False  # 不阻断，正常流程继续
    except Exception as e:
        ctx.logger.warning(f"去格式自动模式异常: {e}")
        return False


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/去md\\s*(.*)", handle_strip, priority=50,
                alias=["/去markdown", "/纯文本"], description="去除 Markdown 语法")
    ctx.command("/去md帮助\\s*$", handle_help, priority=50, description="去格式插件帮助")
    ctx.command("^去格式\\s+(开|关)\\s*$", handle_toggle, priority=50,
                description="群内开关自动去格式")
    ctx.on_raw_message(on_raw)
    ctx.logger.info("去格式插件注册完成")

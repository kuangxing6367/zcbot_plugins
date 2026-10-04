"""
戳一戳回复插件 (poke)
====================
  当有人戳机器人（或群里互相戳且开启响应）时，机器人以趣味方式回应：
  - 默认随机回复一句戳回去的俏皮话
  - 可配置「反戳」：直接调用 send_poke 戳回对方
  - 支持自定义回应语料（配置文件 poke_replies.txt 或 _conf_schema 的 replies）
事件来源：OneBot 11 notice(notify/poke)。因框架事件总线命名不保证，
这里用 on_raw_message 直接识别原始戳一戳事件，最稳健。
"""
import re
import random

__plugin_meta__ = {
    "name": "戳一戳回复",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "有人戳机器人时趣味回应，可开启反戳或自定义语料",
    "priority": 50,
}

ctx = None

_DEFAULT_REPLIES = [
    "你戳我干嘛？再戳我可要生气了！",
    "戳到了我的痒痒肉，嘿嘿嘿~",
    "别戳啦，再戳我就戳回去哦！",
    "喵？谁在戳我 (・ω・)",
    "戳一戳，好运来~",
    "你戳我一下，我记你一辈子（划掉）",
]

_CFG_TTL = 60
_cache = {"t": 0, "data": None}


def _cfg(key, default):
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


def _load_bool(key, default):
    v = _cfg(key, default)
    return str(v).lower() in ("1", "true", "yes", "on") if v is not None else default


def _load_list(key):
    v = _cfg(key, "")
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
    return []


def _is_poke(raw: dict) -> bool:
    """识别原始戳一戳事件"""
    if raw.get("post_type") != "notice":
        return False
    # OneBot 11 标准：notify + sub_type=poke
    if raw.get("notice_type") == "notify" and raw.get("sub_type") == "poke":
        return True
    # 部分实现直接 notice_type=poke
    if raw.get("notice_type") == "poke":
        return True
    return False


async def on_raw(raw: dict, bot_name: str) -> bool:
    """原始消息/事件接管：识别戳一戳并回应"""
    try:
        if not _is_poke(raw):
            return False
        self_id = str(raw.get("self_id", ""))
        target_id = str(raw.get("target_id", ""))
        user_id = raw.get("user_id", 0)
        group_id = raw.get("group_id") or 0
        # 仅响应「戳机器人」的事件
        if target_id and target_id != self_id:
            return False
        if not user_id or not self_id:
            return False

        reverse = _load_bool("reverse_poke", False)
        if reverse and user_id and user_id != int(self_id):
            try:
                await ctx.aapi(
                    "send_poke", user_id=int(user_id),
                    group_id=group_id or None, bot=bot_name,
                )
            except Exception as e:
                ctx.logger.warning(f"反戳失败: {e}")
            # 反戳后不再发文字，避免刷屏
            return True

        replies = _load_list("replies") or _DEFAULT_REPLIES
        text = random.choice(replies)
        await ctx.asend_msg(
            user_id=int(user_id),
            group_id=group_id or None,
            message=text,
            bot=bot_name,
        )
        return True
    except Exception as e:
        ctx.logger.warning(f"戳一戳处理异常: {e}")
        return False


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.on_raw_message(on_raw)
    ctx.logger.info("戳一戳插件注册完成")

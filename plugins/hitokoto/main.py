"""
一言插件 - 群内/私聊发送"一言"，从 Hitokoto API 获取一句随机的话并回复
API: https://v1.hitokoto.cn/  (返回 JSON，含 hitokoto 正文 / from 出处 / from_who 作者)
"""
import requests

__plugin_meta__ = {
    "name": "一言",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "发送\"一言\"获取随机一言（来自 v1.hitokoto.cn）",
    "priority": 50,
}

HITOKOTO_API = "https://v1.hitokoto.cn/"
SENSITIVE_API = "https://v.api.aa1.cn/api/api-mgc/index.php"
TIMEOUT = 5

# 敏感词 API 返回 JSON 中可能承载结果的字段（按优先级取值）
SENSITIVE_RESULT_KEYS = ("msg", "data", "result", "message", "content", "text")


def register(ctx):
    """注册命令：pattern 不带 /，可同时匹配「一言」与「/一言」；「检测」与「/检测」"""
    ctx.command("一言", handle_hitokoto, priority=50,
                description="获取一句随机一言（Hitokoto）")
    ctx.command("检测", handle_sensitive, priority=50,
                description="敏感词检测：发送「检测 内容」调用 aa1 敏感词 API")


def handle_hitokoto(event, match):
    """发送"一言"时请求 Hitokoto API，解析并回复正文 + 出处"""
    try:
        resp = requests.get(HITOKOTO_API, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        ctx.log(f"一言请求失败: {e}", level="error")
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message="一言获取失败，请稍后再试~",
        )
        return

    # 解析返回字段（缺失时兜底为空串，保证回复不报错）
    sentence = data.get("hitokoto", "") or ""
    source = data.get("from", "") or ""
    author = data.get("from_who", "") or ""

    tail = " · ".join([p for p in (source, author) if p])

    reply = sentence
    if tail:
        reply += f"\n—— {tail}"

    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=reply,
    )



def handle_sensitive(event, match):
    """敏感词检测：发送「检测 内容」，GET 请求 aa1 敏感词 API 并回复结果

    用法示例：检测 今天天气不错
    返回格式未知，做容错解析：优先从常见 JSON 字段提取结果，
    解析失败时直接回显原始响应文本（截断防刷屏）。
    """
    # 提取待检测文本（match 参数 / event.message 兜底）
    text = ""
    if match:
        text = match.group(1).strip()
    if not text:
        msg = event.message or ""
        for prefix in ("检测", "/检测"):
            if msg.startswith(prefix):
                text = msg[len(prefix):].strip()
                break

    if not text:
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message="用法：发送「检测 内容」进行敏感词检测，例如：检测 今天天气不错",
        )
        return

    try:
        resp = requests.get(SENSITIVE_API, params={"msg": text}, timeout=TIMEOUT)
        resp.raise_for_status()
        # 兼容多种编码：优先 utf-8，失败回退 gbk
        try:
            body = resp.text
            data = resp.json()
        except Exception:
            body = resp.content.decode("utf-8", errors="replace")
            data = None
    except Exception as e:
        ctx.log(f"敏感词检测请求失败: {e}", level="error")
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message="敏感词检测失败，请稍后再试~",
        )
        return

    reply = ""
    if isinstance(data, dict):
        # 从常见字段中取第一个非空字符串结果
        for key in SENSITIVE_RESULT_KEYS:
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                reply = val.strip()
                break
            if isinstance(val, (int, float)):
                reply = str(val)
                break
        if not reply:
            reply = str(data)
    elif isinstance(data, (list, tuple)):
        reply = "、".join(str(x) for x in data if x not in (None, "")) or str(data)
    else:
        reply = body.strip()

    # 截断过长响应，防止刷屏
    if len(reply) > 300:
        reply = reply[:300] + "…"

    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=f"检测结果：{reply}",
    )

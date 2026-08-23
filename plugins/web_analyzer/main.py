"""
网页分析插件 (web_analyzer)
===========================
功能（从 AstrBot「web_analyzer」类插件迁移，功能逻辑重写）：
  /分析 <url>     -> 抓取网页，提取标题/描述/站点/正文字数，并做简单风险标注
  /分析开 / 分析关 -> 群内开关「自动分析模式」：群消息含链接时自动回复解析卡片
数据来源：httpx 抓取 + 基础 HTML meta 解析（无外部 AI 依赖，离线可用）。
"""
import re
from urllib.parse import urlparse

import httpx

__plugin_meta__ = {
    "name": "网页分析",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "抓取网页提取标题/描述/风险标注，支持群内自动分析",
    "priority": 50,
}

ctx = None

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ZCBotWebAnalyzer/1.0)"}
_RISK_DOMAINS = ("login", "account", "secure", "verify", "pay", "bank", "captcha")
_RISK_KW = ("中奖", "领取红包", "免费领", "点击领取", "验证码", "账户异常")


def _extract_meta(html: str):
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        title = m.group(1).strip()
    desc = ""
    m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', html, re.S | re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']', html, re.S | re.I)
    if m:
        desc = m.group(1).strip()
    og = ""
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']', html, re.S | re.I)
    if m:
        og = m.group(1).strip()
    return title, desc, og


def _risk(html: str, host: str):
    flags = []
    low = (html or "").lower()
    for kw in _RISK_KW:
        if kw in (html or ""):
            flags.append(kw)
    for d in _RISK_DOMAINS:
        if d in (host or ""):
            flags.append(f"域名含'{d}'")
    return flags


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def analyze(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15, headers=_HEADERS, follow_redirects=True) as client:
            r = await client.get(url)
            html = r.text
        title, desc, og = _extract_meta(html)
        host = urlparse(str(r.url)).netloc
        lines = [
            f"🔗 网页分析：{host}",
            f"标题：{title or og or '(无)'}",
            f"描述：{(desc or '(无)')[:200]}",
            f"正文长度：{len(html)} 字符",
        ]
        flags = _risk(html, host)
        if flags:
            lines.append("⚠️ 风险提示：" + "、".join(flags[:5]))
        else:
            lines.append("✅ 未发现明显风险关键词")
        return "\n".join(lines)
    except Exception as e:
        return f"分析失败（网络/解析异常）: {e}"


async def handle_analyze(event, match):
    """分析 <url>"""
    url = (match.group(1) if match else "").strip()
    if not url.startswith("http"):
        await _reply(event, "用法: /分析 <网页URL>")
        return
    await _reply(event, await analyze(url))


async def handle_toggle(event, match):
    """分析开/关"""
    if not getattr(event, "is_group", False):
        await _reply(event, "请在群聊中使用")
        return
    if not (getattr(event, "role", "") in ("admin", "owner", "super")):
        await _reply(event, "需要管理员权限")
        return
    on = "开" in (event.message or "")
    key = f"weban_auto_{event.group_id}"
    try:
        ctx.db_execute(
            "INSERT INTO plugin_configs (plugin_name, config_key, config_value) "
            "VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE config_value=%s",
            ["web_analyzer", key, "1" if on else "0", "1" if on else "0"],
        )
    except Exception:
        try:
            ctx.db_execute(
                "INSERT OR REPLACE INTO plugin_configs (plugin_name, config_key, config_value) "
                "VALUES (%s,%s,%s)", ["web_analyzer", key, "1" if on else "0"],
            )
        except Exception as e:
            await _reply(event, f"设置失败: {e}")
            return
    await _reply(event, f"自动网页分析已{'开启' if on else '关闭'}")


_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff]+", re.I)


async def on_raw(raw: dict, bot_name: str) -> bool:
    if raw.get("post_type") != "message" or raw.get("message_type") != "group":
        return False
    gid = raw.get("group_id")
    if not gid:
        return False
    row = ctx.db_query_one(
        "SELECT config_value FROM plugin_configs WHERE plugin_name=%s AND config_key=%s",
        ["web_analyzer", f"weban_auto_{gid}"],
    )
    if not row or str(row.get("config_value")) != "1":
        return False
    msg = raw.get("message", "")
    if isinstance(msg, list):
        msg = "".join(s.get("data", {}).get("text", "") for s in msg
                      if isinstance(s, dict) and s.get("type") == "text")
    url = _URL_RE.search(str(msg or ""))
    if not url:
        return False
    text = await analyze(url.group(0))
    await ctx.asend_msg(user_id=raw.get("user_id"), group_id=gid, message=text, bot=bot_name)
    return False


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/分析\\s+([\\s\\S]+)", handle_analyze, priority=50, description="分析网页")
    ctx.command("^分析\\s+(开|关)\\s*$", handle_toggle, priority=50,
                description="群内开关自动分析", require_admin=True)
    ctx.on_raw_message(on_raw)
    ctx.logger.info("网页分析插件注册完成")

"""
QQ空间插件 (qzone)
====================
功能（从 AstrBot「qzone」类插件迁移，功能逻辑重写）：
  /说说 <文本>   -> 以机器人账号发布一条 QQ 空间说说（需配置 QZone Cookie）
  /说说列表      -> 读取机器人空间最近说说
  /空间帮助      -> 说明
注意：QQ 空间接口需要登录态。请在插件配置中填写 qzone_uin（机器人QQ）与
qzone_cookie（完整 Cookie）。g_tk 由 Cookie 中的 p_skey 经公开哈希算法推导。
未配置时命令会给出明确提示，不会崩溃。
"""
import re
import time
from urllib.parse import quote

import httpx

__plugin_meta__ = {
    "name": "QQ空间",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "以机器人账号发布/读取 QQ 空间说说（需配置 QZone Cookie）",
    "priority": 50,
}

ctx = None

_PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_publish_v6"
_FEED_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"


def _cfg(key, default):
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


def _g_tk(cookie: str) -> str:
    """由 Cookie 中 p_skey 推导 g_tk（公开哈希算法）。"""
    p_skey = ""
    for part in (cookie or "").split(";"):
        part = part.strip()
        if part.startswith("p_skey="):
            p_skey = part[len("p_skey="):]
            break
    if not p_skey:
        return "0"
    h = 5381
    for ch in p_skey:
        h += (h << 5) + ord(ch)
    return str(h & 0x7FFFFFFF)


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


def _cookies_dict(cookie: str):
    d = {}
    for part in (cookie or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            d[k] = v
    return d


async def handle_publish(event, match):
    """说说 <文本>"""
    uin = _cfg("qzone_uin", "")
    cookie = _cfg("qzone_cookie", "")
    if not uin or not cookie:
        await _reply(event, "尚未配置 QZone 凭证。请在插件配置填写 qzone_uin 与 qzone_cookie。")
        return
    text = (match.group(1) if match else "").strip()
    if not text:
        await _reply(event, "用法: /说说 <要说的内容>")
        return
    g_tk = _g_tk(cookie)
    data = {
        "synchost": "https://user.qzone.qq.com",
        "format": "json",
        "qzreferrer": f"https://user.qzone.qq.com/{uin}",
        "content": text,
        "richval": "",
        "richtype": "",
        "to_tweet": "0",
        "code_version": "1",
        "subcode": "1",
        "g_tk": g_tk,
    }
    try:
        async with httpx.AsyncClient(timeout=20, headers={"Cookie": cookie, "Referer": f"https://user.qzone.qq.com/{uin}"}, follow_redirects=True) as client:
            r = await client.post(_PUBLISH_URL, data=data)
            # 返回可能是 callback({...}) 或 json
            body = r.text
            if "callback(" in body:
                body = body.split("(", 1)[1].rsplit(")", 1)[0]
            import json
            js = json.loads(body)
        if js.get("code") == 0:
            await _reply(event, "✅ 已发布到 QQ 空间")
        else:
            await _reply(event, f"发布失败：{js.get('message', body)[:200]}")
    except Exception as e:
        await _reply(event, f"发布异常: {e}")


async def handle_list(event, match):
    """说说列表"""
    uin = _cfg("qzone_uin", "")
    cookie = _cfg("qzone_cookie", "")
    if not uin or not cookie:
        await _reply(event, "尚未配置 QZone 凭证。")
        return
    g_tk = _g_tk(cookie)
    params = {
        "uin": uin, "ftype": "0", "sort": "0", "pos": "0", "num": "10",
        "g_tk": g_tk, "format": "json", "code_version": "1", "subcode": "1",
    }
    try:
        async with httpx.AsyncClient(timeout=20, headers={"Cookie": cookie, "Referer": f"https://user.qzone.qq.com/{uin}"}, follow_redirects=True) as client:
            r = await client.get(_FEED_URL, params=params)
            body = r.text
            if "callback(" in body:
                body = body.split("(", 1)[1].rsplit(")", 1)[0]
            import json
            js = json.loads(body)
        if js.get("code") != 0:
            await _reply(event, f"读取失败：{js.get('message', '')[:200]}")
            return
        msglist = (js.get("msglist") or [])[:10]
        if not msglist:
            await _reply(event, "空间暂无说说")
            return
        lines = ["📝 最近说说："]
        for m in msglist:
            lines.append(f"  · {m.get('content', '')[:60]}  ({m.get('created_time', '')})")
        await _reply(event, "\n".join(lines))
    except Exception as e:
        await _reply(event, f"读取异常: {e}")


async def handle_help(event, match):
    await _reply(event,
        "🌐 QQ空间插件（需配置 qzone_uin / qzone_cookie）\n"
        "▸ /说说 <内容>  → 发布说说\n"
        "▸ /说说列表     → 读取最近说说\n"
        "▸ /空间帮助")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/说说\\s+([\\s\\S]+)", handle_publish, priority=50, description="发布 QQ 空间说说")
    ctx.command("/说说列表\\s*$", handle_list, priority=50, description="读取最近说说")
    ctx.command("/空间帮助\\s*$", handle_help, priority=50, description="QQ空间帮助")
    ctx.logger.info("QQ空间插件注册完成")

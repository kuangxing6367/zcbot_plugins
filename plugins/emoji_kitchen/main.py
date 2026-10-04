"""
Emoji 融合插件 (emoji_kitchen)
==============================
  /表情融合 <emoji1> <emoji2>  -> 调用 Emoji Kitchen 生成两张 emoji 的组合贴纸并返回图片
  /emoji <e1> <e2>             -> 同上（别名）
数据来源：Emoji Kitchen 公共接口 api.emojikitchen.dev。
"""
import re

import httpx

__plugin_meta__ = {
    "name": "Emoji 融合",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "将两张 emoji 融合为组合贴纸图片",
    "priority": 50,
}

ctx = None

_API = "https://api.emojikitchen.dev/v1/{a}/{b}"
_HEADERS = {"User-Agent": "emoji-kitchen-bot"}


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def handle_fuse(event, match):
    """表情融合 <e1> <e2>"""
    text = (match.group(1) if match else "").strip()
    emojis = re.findall(r"\s(\S)\s", " " + text + " ") or re.findall(r"(\S)", text)
    # 取前两个非空字符（emoji 可能多字节，按字素近似分割）
    parts = [p for p in re.split(r"\s+", text) if p]
    if len(parts) < 2:
        await _reply(event, "用法: /表情融合 <emoji1> <emoji2>，如 /表情融合 😎 🔥")
        return
    a, b = parts[0], parts[1]
    url = None
    for order in ((a, b), (b, a)):
        try:
            async with httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
                r = await client.get(_API.format(a=order[0], b=order[1]))
                if r.status_code == 200:
                    data = r.json()
                    results = data.get("results") or []
                    if results:
                        url = results[0].get("url") or results[0].get("image")
                        if url:
                            break
        except Exception:
            continue
    if not url:
        await _reply(event, "未找到该组合（Emoji Kitchen 仅支持部分组合）")
        return
    try:
        await ctx.asend_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message=f"[CQ:image,file={url}]",
        )
    except Exception as e:
        await _reply(event, f"发送图片失败: {e}")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/表情融合\\s+([\\s\\S]+)", handle_fuse, priority=50, description="融合两张 emoji")
    ctx.command("/emoji\\s+([\\s\\S]+)", handle_fuse, priority=50, description="融合两张 emoji（别名）")
    ctx.logger.info("Emoji 融合插件注册完成")

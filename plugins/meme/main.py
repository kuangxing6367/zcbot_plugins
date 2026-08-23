"""
表情包生成插件 (meme)
========================
功能（从 AstrBot「mememaker_api」类插件迁移，功能逻辑重写）：
  /meme <模板> <上文字> <下文字>  -> 调用 MemeGen 公共接口生成表情包图片并返回
  /meme模板                     -> 列出几个常用模板名
数据来源：api.memegen.link（公共 Meme 生成服务，离线不可用时提示）。
"""
import re
from urllib.parse import quote

import httpx

__plugin_meta__ = {
    "name": "表情包生成",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "调用 MemeGen 生成上下文字表情包图片",
    "priority": 50,
}

ctx = None

_BASE = "https://api.memegen.link/images/{template}/{top}/{bottom}"
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_SAMPLES = ["drake", "doge", "buzz", "success-kid", " RollSafe", "grumpy-cat", "wonka", "distracted-boyfriend"]


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


def _clean(s):
    # MemeGen 用 _ 表示空格，- 表示占位；简单清洗
    return quote((s or "").replace(" ", "_"))


async def handle_meme(event, match):
    """meme <模板> <上> <下>"""
    text = (match.group(1) if match else "").strip()
    parts = text.split(None, 2)
    if len(parts) < 3:
        await _reply(event, "用法: /meme <模板名> <上文字> <下文字>，如 /meme drake 拒绝 接受")
        return
    template, top, bottom = parts[0], parts[1], parts[2]
    url = _BASE.format(template=template, top=_clean(top), bottom=_clean(bottom))
    try:
        async with httpx.AsyncClient(timeout=25, headers=_HEADERS, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            # 直接转发图片（MemeGen 返回 PNG）
            await ctx.asend_msg(
                user_id=event.user_id,
                group_id=event.group_id if event.is_group else None,
                message=f"[CQ:image,file={str(r.url)}]",
            )
    except Exception as e:
        await _reply(event, f"生成失败（模板名或接口异常）: {e}")


async def handle_templates(event, match):
    await _reply(event, "常用模板：" + "、".join(t.strip() for t in _SAMPLES) + "\n（更多见 https://memegen.link）")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/meme\\s+([\\s\\S]+)", handle_meme, priority=50, description="生成表情包")
    ctx.command("/meme模板\\s*$", handle_templates, priority=50, description="表情包模板列表")
    ctx.logger.info("表情包生成插件注册完成")

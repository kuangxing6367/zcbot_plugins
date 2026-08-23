"""
名言金句插件 (hjm)
====================
功能（从 AstrBot「zhiyu_astrbot_hjm」类插件迁移，功能逻辑重写）：
  /名言     -> 随机返回一句名言/金句
  /名言 <关键词> -> 尝试返回包含关键词的句子（无则随机）
  /名言帮助 -> 说明
语料为本地内置，离线可用。
"""
import re
import random

__plugin_meta__ = {
    "name": "名言金句",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "随机/按关键词返回名言金句（本地语料）",
    "priority": 50,
}

ctx = None

_QUOTES = [
    "路漫漫其修远兮，吾将上下而求索。",
    "不积跬步，无以至千里。",
    "业精于勤，荒于嬉；行成于思，毁于随。",
    "世上无难事，只要肯登攀。",
    "千里之行，始于足下。",
    "知之者不如好之者，好之者不如乐之者。",
    "三人行，必有我师焉。",
    "会当凌绝顶，一览众山小。",
    "长风破浪会有时，直挂云帆济沧海。",
    "宝剑锋从磨砺出，梅花香自苦寒来。",
    "落霞与孤鹜齐飞，秋水共长天一色。",
    "海纳百川，有容乃大。",
    "博观而约取，厚积而薄发。",
    "非淡泊无以明志，非宁静无以致远。",
    "天下兴亡，匹夫有责。",
]


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def handle_quote(event, match):
    """名言 [关键词]"""
    kw = (match.group(1) if match else "").strip()
    if kw:
        hits = [q for q in _QUOTES if kw in q]
        if hits:
            await _reply(event, "📜 " + random.choice(hits))
            return
    await _reply(event, "📜 " + random.choice(_QUOTES))


async def handle_help(event, match):
    await _reply(event, "📜 名言金句\n▸ /名言  → 随机名言\n▸ /名言 <词> → 含词名言\n▸ /名言帮助")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/名言\\s*([\\s\\S]*)$", handle_quote, priority=50, description="随机名言")
    ctx.command("/名言帮助\\s*$", handle_help, priority=50, description="名言帮助")
    ctx.logger.info("名言金句插件注册完成")

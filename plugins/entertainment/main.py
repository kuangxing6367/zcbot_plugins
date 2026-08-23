"""
娱乐插件 (entertainment)
=========================
功能（从 AstrBot「entertainment」类插件迁移，功能逻辑重写）：
  /笑话        -> 随机段子（公共接口，失败回退本地语料）
  /绕口令      -> 随机绕口令
  /脑筋急转弯  -> 随机脑筋急转弯（含答案提示）
  /星座 <星座> -> 今日星座运势（本地语料）
  /抽签        -> 随机签文
  /今日运势    -> 按 QQ+日期确定性运势
为保证离线可用，段子/绕口令/星座等以本地语料为主，笑话优先尝试公共接口。
"""
import re
import random
from datetime import date

import httpx

__plugin_meta__ = {
    "name": "娱乐百宝箱",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "笑话/绕口令/脑筋急转弯/星座运势/抽签等娱乐指令",
    "priority": 50,
}

ctx = None

_TONGUE_TWISTERS = [
    ("四是四，十是十，十四是十四，四十是四十，谁能说清四十四。", ""),
    ("红鲤鱼与绿鲤鱼与驴。", ""),
    ("吃葡萄不吐葡萄皮，不吃葡萄倒吐葡萄皮。", ""),
]
_BRAIN_TEASERS = [
    ("什么东西越洗越脏？", "水"),
    ("什么车寸步难行？", "风车"),
    ("什么布剪不断？", "瀑布"),
    ("什么东西人们都不想要，却又不得不接受？", "年龄/变老"),
    ("一个人从十米高的梯子上掉下来却没受伤，为什么？", "他从最低一级掉下来"),
]
_FORTUNE = ["大吉", "中吉", "小吉", "吉", "半吉", "末吉", "凶", "大凶"]
_CONSTELL = ["白羊", "金牛", "双子", "巨蟹", "狮子", "处女", "天秤", "天蝎", "射手", "摩羯", "水瓶", "双鱼"]
_LUCK_WORDS = [
    "今天适合摸鱼，不适合加班。", "桃花运不错，但要注意钱包。",
    "宜学习，忌拖延。", "贵人就在你身边的群里。",
    "今天适合吃点好的犒劳自己。", "诸事顺遂，但别太飘。",
]
_JOKE_API = "https://api.vvhan.com/api/text/joke"


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def handle_joke(event, match):
    """笑话"""
    text = "（本地段子）\n"
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
            r = await c.get(_JOKE_API)
            if r.status_code == 200:
                j = r.json()
                data = j.get("data")
                if isinstance(data, dict):
                    data = data.get("content") or data.get("text")
                if data:
                    text = str(data)
    except Exception:
        pass
    if text.startswith("（本地段子）"):
        local = [
            "许仙给白娘子买帽子，店员问：要不要带角的？许仙：不用，她已经有俩了。",
            "为什么程序员总分不清万圣节和圣诞节？因为 Oct 31 == Dec 25。",
            "我妈说我是充话费送的，我说那我弟呢？她说你弟是买一送一。",
        ]
        text += random.choice(local)
    await _reply(event, "😄 " + text)


async def handle_tongue(event, match):
    await _reply(event, "👅 绕口令：\n" + random.choice(_TONGUE_TWISTERS)[0])


async def handle_teaser(event, match):
    q, a = random.choice(_BRAIN_TEASERS)
    await _reply(event, f"🤔 脑筋急转弯：{q}\n（答案：{a}）")


async def handle_constell(event, match):
    """星座 <星座>"""
    text = (match.group(1) if match else "").strip()
    name = None
    for c in _CONSTELL:
        if c in text:
            name = c
            break
    if not name:
        await _reply(event, "用法: /星座 <星座名>，如 /星座 狮子")
        return
    lw = random.choice(_LUCK_WORDS)
    star = random.choice(_FORTUNE)
    await _reply(event, f"🔮 {name}座今日运势：{star}\n{lw}")


async def handle_draw(event, match):
    """抽签"""
    lines = ["🎏 今日签文："]
    lines.append(random.choice(_FORTUNE))
    lines.append(random.choice(_LUCK_WORDS))
    await _reply(event, "\n".join(lines))


async def handle_daily(event, match):
    """今日运势"""
    import hashlib
    seed = hashlib.md5(f"{event.user_id}{date.today().isoformat()}".encode()).hexdigest()
    idx = int(seed[:2], 16) % len(_FORTUNE)
    await _reply(event, f"🍀 你的今日运势：{_FORTUNE[idx]}\n{random.choice(_LUCK_WORDS)}")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("^/笑话\\s*$", handle_joke, priority=50, description="随机笑话")
    ctx.command("^/绕口令\\s*$", handle_tongue, priority=50, description="随机绕口令")
    ctx.command("^/脑筋急转弯\\s*$", handle_teaser, priority=50, description="脑筋急转弯")
    ctx.command("^/星座\\s+([\\s\\S]+)", handle_constell, priority=50, description="星座运势")
    ctx.command("^/抽签\\s*$", handle_draw, priority=50, description="抽签")
    ctx.command("^/今日运势\\s*$", handle_daily, priority=50, description="今日运势")
    ctx.logger.info("娱乐插件注册完成")

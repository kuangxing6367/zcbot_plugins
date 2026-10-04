# -*- coding: utf-8 -*-
"""娱乐语料包 (funbox)
=====================
本地语料娱乐合集，除 /笑话 的可选在线接口外全部离线可用：

  /笑话          随机笑话（在线接口可选，失败自动降级本地语料并轻提示）
  /绕口令        随机绕口令
  /脑筋急转弯    随机脑筋急转弯（附答案）
  /星座 <星座>   星座今日运势（本地语料）
  /抽签          抽取今日签文（卡片图）
  /今日运势      按 QQ+日期确定的今日运势（卡片图）
  /名言 [词]     随机/按关键词名言金句（卡片图）
  /名言帮助      名言用法说明
  /测试          开始趣味测试（多轮问答，会话等待器接管）
  /答 <选项>     作答当前题目
  /测试结果      查看答题进度
  /测试帮助      测试用法说明
  /cp @A @B      计算两人羁绊值（卡片图）
  /关系 QQ1 QQ2  同 /cp 的数字 QQ 形式
  /今日缘分 [@用户]  与指定用户的今日缘分值（卡片图）
  /娱乐帮助      本插件总说明

卡片图输出优先走共享图片渲染引擎（sys.modules.get("plugin_image_renderer")），
渲染器未安装或渲染失败时自动回退同内容文本，命令不会因此失败。
/cp /关系 /今日缘分 的数值由 QQ 号对 + 日期做确定性哈希，同一对名字结果稳定。
"""
import asyncio
import hashlib
import os
import random
import re
import sys
import tempfile
from datetime import date

import httpx

import corpus
import quiz

__plugin_meta__ = {
    "name": "娱乐语料包",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "本地语料娱乐合集：笑话/绕口令/脑筋急转弯/星座/抽签/今日运势/名言/趣味测试/CP 缘分",
    "priority": 70,
}

ctx = None

# 关系标签语料（小体量，直接内置）
_LABELS_HIGH = ["天作之合", "灵魂伴侣", "天生一对", "命中注定", "如胶似漆"]
_LABELS_MID = ["颇有好感", "默契搭档", "欢喜冤家", "渐入佳境", "最佳拍档"]
_LABELS_LOW = ["点头之交", "塑料情谊", "欢喜冤家", "尚需磨合", "友谊小船"]

# 笑话在线接口默认地址（可在配置中修改或关闭）
_DEFAULT_JOKE_API = "https://api.vvhan.com/api/text/joke"
_DEFAULT_FALLBACK_TIP = "（网络接口开小差了，来个本地的）"

SUITE_HELP = """🎈 娱乐语料包
▸ /笑话 —— 随机笑话
▸ /绕口令 —— 随机绕口令
▸ /脑筋急转弯 —— 脑筋急转弯（附答案）
▸ /星座 <星座名> —— 星座今日运势
▸ /抽签 —— 抽取今日签文
▸ /今日运势 —— 查看你的今日运势
▸ /名言 [词] —— 随机/按关键词名言金句
▸ /测试 → /答 A-D → /测试结果 —— 多轮趣味测试
▸ /cp @用户A @用户B 或 /关系 QQ1 QQ2 —— 两人羁绊值
▸ /今日缘分 [@用户] —— 今日缘分值
▸ /娱乐帮助 —— 本说明"""


# ---------------- 基础工具 ----------------

def _conf(key, default=None):
    """读取插件配置（Web UI 配置项，带默认值兜底）"""
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


def _feature_on(key):
    return bool(_conf(key, True))


async def _reply(event, text):
    """命令流程回复（自动判断私聊/群聊）"""
    try:
        await ctx.asend_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message=text,
        )
    except Exception as e:
        ctx.log(f"回复消息失败: {e}", level="warning")


def _guarded(switch):
    """按功能开关包装 async 命令 handler：开关关闭时轻提示后直接返回"""
    def deco(fn):
        async def wrapper(event, match):
            if not _feature_on(switch):
                await _reply(event, "（该功能已在插件配置中关闭）")
                return
            await fn(event, match)
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


# ---------------- 卡片图输出（共享渲染引擎优先，回退文本） ----------------

def _renderer():
    """获取共享图片渲染器模块（未安装/未加载时返回 None）"""
    mod = sys.modules.get("plugin_image_renderer")
    if mod is not None and hasattr(mod, "_render_card_image"):
        return mod
    return None


async def _send_card(event, title, lines, width=560):
    """把若干行文本渲染成卡片图发送；任一步失败返回 False，由调用方回退文本"""
    mod = _renderer()
    if mod is None:
        return False
    content = "\n".join(str(x) for x in lines)
    try:
        # 渲染属 CPU 密集操作，丢线程池执行，避免阻塞事件循环
        result = await asyncio.wrap_future(
            ctx.run_async(mod._render_card_image, title, content, width))
    except Exception as e:
        ctx.log(f"卡片渲染失败，回退文本: {e}", level="warning")
        return False
    if result is None:
        return False

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        if isinstance(result, (bytes, bytearray)):
            with open(tmp_path, "wb") as f:
                f.write(bytes(result))
        else:
            # PIL Image 对象
            result.save(tmp_path, "PNG")
        # Windows 临时路径的反斜杠需转成正斜杠才能被 OneBot 客户端解析
        path_str = tmp_path.replace("\\", "/")
        await ctx.asend_msg(
            user_id=event.user_id,
            group_id=event.group_id if getattr(event, "is_group", False) else None,
            message=f"[CQ:image,file=file:///{path_str}]",
        )
        return True
    except Exception as e:
        ctx.log(f"卡片图发送失败，回退文本: {e}", level="warning")
        return False
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


async def _card_or_text(event, title, lines, width=560):
    """卡片图优先输出；总开关关闭或渲染不可用时回退同内容文本"""
    if _feature_on("card_enable") and await _send_card(event, title, lines, width):
        return
    await _reply(event, "\n".join(str(x) for x in lines))


# ---------------- 笑话 / 绕口令 / 脑筋急转弯 ----------------

async def _fetch_joke():
    """请求在线笑话接口；任何异常返回空串（调用方降级本地语料，不抛错）"""
    url = str(_conf("joke_api_url", _DEFAULT_JOKE_API) or "").strip()
    if not url:
        return ""
    try:
        timeout = float(_conf("joke_api_timeout", 8) or 8)
    except (TypeError, ValueError):
        timeout = 8.0
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return ""
            j = r.json()
            data = j.get("data")
            if isinstance(data, dict):
                data = data.get("content") or data.get("text")
            return str(data).strip() if data else ""
    except Exception as e:
        ctx.log(f"笑话接口请求失败，降级本地语料: {e}", level="info")
        return ""


@_guarded("enable_jokes")
async def handle_joke(event, match):
    """/笑话：在线接口优先，失败必降级本地语料并附轻提示；
    接口被配置关闭时视为本地模式，不附失败提示"""
    text = ""
    failed = False
    if _feature_on("joke_api_enable"):
        text = await _fetch_joke()
        failed = not text  # 尝试过在线接口但没拿到内容，才视为失败
    if text:
        await _reply(event, "😄 " + text)
        return
    local = [j for j in corpus.jokes() if j]
    if not local:
        await _reply(event, "😄 今天也要开心呀。")
        return
    body = "😄 " + random.choice(local)
    if failed:
        tip = str(_conf("joke_fallback_hint", _DEFAULT_FALLBACK_TIP) or "")
        body += tip
    await _reply(event, body)


@_guarded("enable_jokes")
async def handle_tongue(event, match):
    """/绕口令"""
    items = [t for t in corpus.tongue_twisters() if t]
    if not items:
        await _reply(event, "👅 绕口令语料为空")
        return
    await _reply(event, "👅 绕口令：\n" + random.choice(items))


@_guarded("enable_jokes")
async def handle_teaser(event, match):
    """/脑筋急转弯"""
    items = corpus.brain_teasers()
    if not items:
        await _reply(event, "🤔 脑筋急转弯语料为空")
        return
    item = random.choice(items)
    await _reply(event,
                 f"🤔 脑筋急转弯：{item.get('q', '')}\n（答案：{item.get('a', '')}）")


# ---------------- 星座 / 抽签 / 今日运势 ----------------

@_guarded("enable_fortune")
async def handle_constell(event, match):
    """/星座 <星座名>：本地语料运势"""
    text = (match.group(1) if match else "").strip()
    name = None
    for c in corpus.constellations():
        if str(c) in text:
            name = str(c)
            break
    if not name:
        await _reply(event, "用法: /星座 <星座名>，如 /星座 狮子")
        return
    await _reply(event,
                 f"🔮 {name}座今日运势：{random.choice(corpus.fortune_levels())}\n"
                 f"{random.choice(corpus.luck_words())}")


@_guarded("enable_fortune")
async def handle_draw(event, match):
    """/抽签：随机签文（卡片图）"""
    lines = [f"签文：{random.choice(corpus.fortune_levels())}",
             random.choice(corpus.luck_words())]
    await _card_or_text(event, "🎏 今日签文", lines)


@_guarded("enable_fortune")
async def handle_daily(event, match):
    """/今日运势：按 QQ+日期确定性哈希，同一天结果稳定（卡片图）"""
    levels = corpus.fortune_levels()
    seed = hashlib.md5(f"{event.user_id}{date.today().isoformat()}".encode()).hexdigest()
    idx = int(seed[:2], 16) % len(levels)
    lines = [f"运势：{levels[idx]}",
             random.choice(corpus.luck_words())]
    await _card_or_text(event, "🍀 今日运势", lines)


# ---------------- 名言金句 ----------------

@_guarded("enable_quotes")
async def handle_quote(event, match):
    """/名言 [关键词]：随机或按关键词出句（卡片图）"""
    kw = (match.group(1) if match else "").strip()
    if kw in ("帮助", "help"):
        await handle_quote_help(event, match)
        return
    quotes = [q for q in corpus.quotes() if q]
    if not quotes:
        await _reply(event, "📜 名言语料为空")
        return
    pool = quotes
    if kw:
        hits = [q for q in quotes if kw in q]
        if hits:
            pool = hits
    await _card_or_text(event, "📜 名言金句", [random.choice(pool)])


async def handle_quote_help(event, match):
    """/名言帮助"""
    await _reply(event, "📜 名言金句\n"
                        "▸ /名言 → 随机名言\n"
                        "▸ /名言 <词> → 含关键词的名言\n"
                        "▸ /名言帮助 → 本说明")


# ---------------- 关系 / CP / 今日缘分（确定性哈希） ----------------

def _score(a, b, salt):
    """确定性羁绊分（0~100）：同一对 QQ 同一 salt 结果稳定"""
    key = f"{min(a, b)}-{max(a, b)}-{salt}"
    h = hashlib.md5(key.encode()).hexdigest()
    return int(h[:4], 16) % 101


def _nick(uid):
    """取用户昵称（群名片优先），失败回退 QQ 号"""
    try:
        info = ctx.get_member_info(0, uid) or {}
        return info.get("card") or info.get("nickname") or str(uid)
    except Exception:
        return str(uid)


def _targets(event, text):
    """从 @ 列表与文本数字串中提取目标 QQ 列表"""
    uids = []
    for u in (getattr(event, "at_list", None) or []):
        if str(u).isdigit():
            uids.append(int(u))
    for m in re.findall(r"(\d{5,})", text or ""):
        uids.append(int(m))
    return uids


def _bond_label(score):
    """按分值段取关系标签"""
    if score >= 80:
        return _LABELS_HIGH[score % len(_LABELS_HIGH)]
    if score >= 50:
        return _LABELS_MID[score % len(_LABELS_MID)]
    return _LABELS_LOW[score % len(_LABELS_LOW)]


@_guarded("enable_bond")
async def handle_cp(event, match):
    """/cp @A @B、/关系 QQ1 QQ2：两人当日羁绊值（卡片图）"""
    text = (match.group(1) if match else "") or (event.message or "")
    uids = _targets(event, text)
    if len(uids) < 2:
        await _reply(event, "用法: /cp @用户A @用户B  或  /关系 <QQ1> <QQ2>")
        return
    a, b = uids[0], uids[1]
    today = date.today().isoformat()
    score = _score(a, b, today)
    na, nb = _nick(a), _nick(b)
    bar = "█" * (score // 5) + "░" * (20 - score // 5)
    lines = [f"{na} × {nb}",
             f"羁绊值：{score}/100",
             f"[{bar}]",
             f"关系标签：{_bond_label(score)}"]
    await _card_or_text(event, "💞 羁绊值", lines)


@_guarded("enable_bond")
async def handle_daily_fate(event, match):
    """/今日缘分 [@用户]：与指定用户的当日缘分值（卡片图）"""
    text = (match.group(1) if match else "") or (event.message or "")
    uids = _targets(event, text)
    target = uids[0] if uids else event.user_id
    today = date.today().isoformat()
    score = _score(event.user_id, target, "daily-" + today)
    lines = [f"你（{_nick(event.user_id)}）与 {_nick(target)}",
             f"今日缘分值：{score}/100"]
    await _card_or_text(event, "🍀 今日缘分", lines)


# ---------------- 总说明 ----------------

async def handle_suite_help(event, match):
    """/娱乐帮助"""
    await _reply(event, SUITE_HELP)


# ---------------- 插件注册 ----------------

def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    corpus.set_logger(ctx.log)
    quiz.init(ctx, _card_or_text)

    # 笑话/绕口令/脑筋急转弯
    ctx.command("^/笑话\\s*$", handle_joke, priority=50,
                description="随机笑话（在线接口优先，本地语料兜底）")
    ctx.command("^/绕口令\\s*$", handle_tongue, priority=50,
                description="随机绕口令")
    ctx.command("^/脑筋急转弯\\s*$", handle_teaser, priority=50,
                description="随机脑筋急转弯（附答案）")

    # 星座/抽签/今日运势
    ctx.command("^/星座\\s+([\\s\\S]+)", handle_constell, priority=50,
                description="星座今日运势")
    ctx.command("^/抽签\\s*$", handle_draw, priority=50,
                description="抽取今日签文")
    ctx.command("^/今日运势\\s*$", handle_daily, priority=50,
                description="查看你的今日运势")

    # 名言金句
    ctx.command("/名言\\s*([\\s\\S]*)$", handle_quote, priority=50,
                description="随机/按关键词名言金句")
    ctx.command("/名言帮助\\s*$", handle_quote_help, priority=50,
                description="名言用法说明")

    # 趣味测试（多轮问答，会话等待器接管）
    ctx.command("^/测试\\s*$", quiz.handle_start, priority=50,
                description="开始趣味测试")
    ctx.command("^/答\\s+([\\s\\S]+)", quiz.handle_answer, priority=50,
                description="回答测试题（A/B/C/D）")
    ctx.command("^/测试结果\\s*$", quiz.handle_progress, priority=50,
                description="查看测试进度")
    ctx.command("^/测试帮助\\s*$", quiz.handle_help, priority=50,
                description="趣味测试说明")

    # 关系/CP/缘分
    ctx.command("^/cp\\s*([\\s\\S]*)", handle_cp, priority=50,
                description="计算两人羁绊值")
    ctx.command("^/关系\\s*([\\s\\S]*)", handle_cp, priority=50,
                description="计算两人关系")
    ctx.command("^/今日缘分\\s*([\\s\\S]*)", handle_daily_fate, priority=50,
                description="查看今日缘分值")

    # 总说明
    ctx.command("^/娱乐帮助\\s*$", handle_suite_help, priority=50,
                description="娱乐语料包总说明")

    ctx.log("娱乐语料包注册完成：笑话/绕口令/脑筋急转弯/星座/抽签/今日运势/"
            "名言/测试/答/测试结果/cp/关系/今日缘分/娱乐帮助")

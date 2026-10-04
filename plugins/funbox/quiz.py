# -*- coding: utf-8 -*-
"""趣味测试问答模块
=================
本地题库多轮问答：/测试 开始 → /答 A/B/C/D 逐题作答 → 累计得分映射结果称号。

作答会话优先接入框架会话等待器（sys.modules.get("plugin_session_waiter")），
由等待器接管用户的下一条「/答 X」消息，逐题循环推进；
等待器不可用时回退插件内自建会话表，由 /答 命令处理流程驱动。
两种路径共用同一份进度状态（按「用户:群」隔离，互不干扰）。
"""
import re
import sys
import threading

import corpus

ctx = None
_card_or_text = None  # 结果卡片输出函数（由插件入口注入）

_LOCK = threading.RLock()
# 进度表："user_id:group_id" -> {"idx": 当前题下标, "points": 累计得分, "total": 总题数}
_SESSIONS = {}

_ANS_RE = re.compile(r"^/答\s*([A-Da-d])\s*$")
_OPTIONS = ("A", "B", "C", "D")

_DEFAULT_WAIT = 90  # 等待作答的默认超时（秒）

HELP_TEXT = ("🦌 趣味测试\n"
             "▸ /测试 → 开始测试\n"
             "▸ /答 A/B/C/D → 作答当前题\n"
             "▸ /测试结果 → 查看进度\n"
             "▸ /测试帮助 → 本说明")


def init(plugin_ctx, card_or_text_fn):
    """注入插件上下文与卡片输出函数（卡片图优先、自动回退文本）"""
    global ctx, _card_or_text
    ctx = plugin_ctx
    _card_or_text = card_or_text_fn


def _conf(key, default):
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


def _enabled():
    return bool(_conf("enable_quiz", True))


def _sw():
    """获取框架会话等待器模块（未加载时返回 None，走自建会话表回退）"""
    return sys.modules.get("plugin_session_waiter")


async def _reply(event, text):
    try:
        await ctx.asend_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message=text,
        )
    except Exception as e:
        ctx.log(f"回复消息失败: {e}", level="warning")


def _state_key(event):
    """进度状态键（与等待器会话键同构：用户:群）"""
    return f"{getattr(event, 'user_id', 0)}:{getattr(event, 'group_id', 0) or 0}"


def _raw_text(raw):
    """从等待器返回的原始事件提取纯文本（message 可能为字符串或消息段数组）"""
    msg = raw.get("message") if isinstance(raw, dict) else None
    if isinstance(msg, list):
        parts = []
        for seg in msg:
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append(str((seg.get("data") or {}).get("text", "")))
        return "".join(parts)
    return str(msg or "")


def _quiz_filter(raw):
    """会话等待器过滤器：消息为「/答 X」作答时返回 True 消费并结束等待；
    其余消息（如 /测试结果、普通聊天）返回 False 放行，不吞消息"""
    return bool(_ANS_RE.match(_raw_text(raw).strip()))


def _question_text(idx, total):
    """渲染第 idx 题的题目文本"""
    q = corpus.quiz_questions()[idx]
    lines = [f"🦌 第 {idx + 1}/{total} 题：", str(q.get("q", ""))]
    lines.extend(str(o) for o in (q.get("opts") or []))
    lines.append("（用 /答 A/B/C/D 作答）")
    return "\n".join(lines)


def _result_of(points):
    """按累计得分映射结果称号（区间未命中时回退默认称号）"""
    text = "🦌 神秘之鹿"
    for r in corpus.quiz_results():
        try:
            if int(r.get("min")) <= points <= int(r.get("max")):
                text = str(r.get("text") or text)
                break
        except (TypeError, ValueError):
            continue
    return text


def _full_score():
    """题库满分（逐题取计分最大值求和；异常时按每题 4 分兜底）"""
    questions = corpus.quiz_questions()
    full = 0
    for q in questions:
        vals = [int(v) for v in (q.get("score") or {}).values()
                if str(v).lstrip("-").isdigit()]
        full += max(vals) if vals else 4
    return full or len(questions) * 4


async def handle_start(event, match):
    """/测试：开始一轮新测试（已有进行中的轮次则提示继续）"""
    if not _enabled():
        await _reply(event, "（趣味测试功能已在插件配置中关闭）")
        return
    key = _state_key(event)
    total = len(corpus.quiz_questions())
    if total <= 0:
        await _reply(event, "题库为空，请联系管理员检查语料文件")
        return
    with _LOCK:
        if key in _SESSIONS:
            await _reply(event, "你已有一轮测试进行中，请用 /答 A/B/C/D 继续作答")
            return
        _SESSIONS[key] = {"idx": 0, "points": 0, "total": total}
    await _reply(event, f"🦌 测试开始！共 {total} 题。\n" + _question_text(0, total))
    if _sw() is not None:
        # 会话等待器接管：本 handler 内逐题等待，直到答完或超时
        await _waiter_loop(event, key)


async def _waiter_loop(event, key):
    """会话等待器路径：循环等待用户下一条「/答 X」消息，答完为止"""
    sw = _sw()
    if sw is None:
        return
    try:
        timeout = float(_conf("quiz_wait_timeout", _DEFAULT_WAIT) or _DEFAULT_WAIT)
    except (TypeError, ValueError):
        timeout = float(_DEFAULT_WAIT)
    while True:
        with _LOCK:
            running = key in _SESSIONS
        if not running:
            return  # 本轮已结束（如被并发触发清理）
        raw = await sw.wait_for_user(ctx, sw.make_session_id(event),
                                     timeout=timeout, handler=_quiz_filter)
        if raw is None:
            with _LOCK:
                _SESSIONS.pop(key, None)
            await _reply(event, "⌛ 答题超时，本轮测试已结束，发送 /测试 可重新开始")
            return
        m = _ANS_RE.match(_raw_text(raw).strip())
        if not m:
            continue  # 过滤器已保证命中，这里兜底继续等待
        if await _apply_answer(event, key, m.group(1).upper()):
            return


async def handle_answer(event, match):
    """/答 <选项>：会话等待器不可用时的作答入口（自建会话表驱动）"""
    if not _enabled():
        await _reply(event, "（趣味测试功能已在插件配置中关闭）")
        return
    key = _state_key(event)
    with _LOCK:
        running = key in _SESSIONS
    if not running:
        await _reply(event, "你还没有开始测试，先发 /测试")
        return
    ans = (match.group(1) if match else "").strip().upper()
    if ans not in _OPTIONS:
        await _reply(event, "请使用 /答 A/B/C/D 作答")
        return
    await _apply_answer(event, key, ans)


async def _apply_answer(event, key, ans):
    """记分并推进进度；答完发送结果卡片。返回 True 表示本轮已结束"""
    finished = False
    points = 0
    total = 0
    nxt = -1
    with _LOCK:
        s = _SESSIONS.get(key)
        if s is None:
            return True
        questions = corpus.quiz_questions()
        if s["idx"] >= len(questions):
            _SESSIONS.pop(key, None)
            return True
        q = questions[s["idx"]]
        s["points"] += int((q.get("score") or {}).get(ans, 0))
        s["idx"] += 1
        total = s["total"]
        if s["idx"] >= total:
            finished = True
            points = s["points"]
            _SESSIONS.pop(key, None)
        else:
            nxt = s["idx"]

    if finished:
        title = "🎉 趣味测试结果"
        lines = [f"得分：{points}/{_full_score()}",
                 _result_of(points),
                 "发送 /测试 可再来一轮"]
        if _card_or_text is not None:
            await _card_or_text(event, title, lines)
        else:
            await _reply(event, "\n".join(lines))
        return True
    await _reply(event, _question_text(nxt, total))
    return False


async def handle_progress(event, match):
    """/测试结果：中途查看当前进度与得分"""
    if not _enabled():
        await _reply(event, "（趣味测试功能已在插件配置中关闭）")
        return
    key = _state_key(event)
    with _LOCK:
        s = dict(_SESSIONS.get(key) or {})
    if not s:
        await _reply(event, "尚未开始测试，发送 /测试 开始")
        return
    await _reply(event,
                 f"📊 测试进度：第 {s['idx'] + 1}/{s['total']} 题，当前得分 {s['points']}")


async def handle_help(event, match):
    """/测试帮助：用法说明"""
    await _reply(event, HELP_TEXT)

"""
趣味测试插件 (deer_quiz)
=========================
功能（从 AstrBot「deer_check」类插件迁移，功能逻辑重写）：
  以问答形式做趣味人格/知识测试：
  /测试       -> 开始测试（本地题库，逐题作答）
  /答 <选项>  -> 回答当前题目（A/B/C/D）
  /测试结果   -> 中途查看进度
  /测试帮助   -> 说明
题库为本地内置，离线可用；每轮测试独立记录进度（按用户）。
"""
import re
import random

__plugin_meta__ = {
    "name": "趣味测试",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "逐题问答的趣味人格/知识测试（本地题库）",
    "priority": 50,
}

ctx = None

# 题库：每题含题干、选项、正确/计分键（这里用累计积分映射结果）
QUESTIONS = [
    {"q": "周末你更想？", "opts": ["A 宅家躺平", "B 出门浪", "C 学习充电", "D 约朋友"], "score": {"A":1,"B":2,"C":3,"D":4}},
    {"q": "面对难题你通常？", "opts": ["A 先放放", "B 找人帮", "C 自己钻研", "D 搜教程"], "score": {"A":1,"B":2,"C":3,"D":4}},
    {"q": "你觉得自己是？", "opts": ["A 社恐", "B 社牛", "C 中间人", "D 独行侠"], "score": {"A":1,"B":2,"C":3,"D":4}},
    {"q": "喝奶茶你选？", "opts": ["A 无糖", "B 三分糖", "C 五分糖", "D 全糖"], "score": {"A":1,"B":2,"C":3,"D":4}},
    {"q": "深夜你在？", "opts": ["A 睡觉", "B 刷剧", "C 加班", "D 蹦迪"], "score": {"A":1,"B":2,"C":3,"D":4}},
]
_RESULTS = {
    range(5, 9): "🦌 佛系小鹿：随遇而安，快乐最重要。",
    range(9, 13): "🌿 平衡之鹿：张弛有度，生活能手。",
    range(13, 17): "🔥 进取之鹿：目标明确，行动力强。",
    range(17, 21): "⚡ 狂野之鹿：能量满满，永动机本机。",
}

# 进度：user_id -> {"idx": int, "points": int, "total": int}
_SESSIONS = {}


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def handle_start(event, match):
    """测试"""
    _SESSIONS[event.user_id] = {"idx": 0, "points": 0, "total": len(QUESTIONS)}
    q = QUESTIONS[0]
    await _reply(event, f"🦌 测试开始！共 {len(QUESTIONS)} 题。\n{q['q']}\n" + "\n".join(q["opts"]) + "\n（用 /答 A 作答）")


async def handle_answer(event, match):
    """答 <选项>"""
    s = _SESSIONS.get(event.user_id)
    if not s:
        await _reply(event, "你还没有开始测试，先发 /测试")
        return
    ans = (match.group(1) if match else "").strip().upper()
    if ans not in ("A", "B", "C", "D"):
        await _reply(event, "请使用 /答 A/B/C/D 作答")
        return
    q = QUESTIONS[s["idx"]]
    s["points"] += q["score"].get(ans, 0)
    s["idx"] += 1
    if s["idx"] >= len(QUESTIONS):
        p = s["points"]
        result = "🦌 神秘之鹿"
        for rng, txt in _RESULTS.items():
            if p in rng:
                result = txt
                break
        _SESSIONS.pop(event.user_id, None)
        await _reply(event, f"🎉 测试完成！得分 {p}/{len(QUESTIONS)*4}\n{result}")
        return
    nq = QUESTIONS[s["idx"]]
    await _reply(event, f"✅ 第 {s['idx']} 题：\n{nq['q']}\n" + "\n".join(nq["opts"]) + "\n（用 /答 A 作答）")


async def handle_progress(event, match):
    s = _SESSIONS.get(event.user_id)
    if not s:
        await _reply(event, "尚未开始测试")
        return
    await _reply(event, f"进度：第 {s['idx']+1}/{s['total']} 题，当前得分 {s['points']}")


async def handle_help(event, match):
    await _reply(event, "🦌 趣味测试\n▸ /测试  → 开始\n▸ /答 A/B/C/D → 作答\n▸ /测试结果 → 进度\n▸ /测试帮助")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("^/测试\\s*$", handle_start, priority=50, description="开始趣味测试")
    ctx.command("^/答\\s+([\\s\\S]+)", handle_answer, priority=50, description="回答测试题")
    ctx.command("^/测试结果\\s*$", handle_progress, priority=50, description="测试进度")
    ctx.command("^/测试帮助\\s*$", handle_help, priority=50, description="测试帮助")
    ctx.logger.info("趣味测试插件注册完成")

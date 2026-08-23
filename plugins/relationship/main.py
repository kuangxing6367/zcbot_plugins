"""
关系 / CP 查询插件 (relationship)
=================================
功能（从 AstrBot「relationship」类插件迁移，功能逻辑重写）：
  /cp @A @B         -> 计算两人「羁绊值」并给出趣味关系标签
  /关系 <QQ1> <QQ2>  -> 同上（数字 QQ 形式）
  /今日缘分 @用户    -> 单人与机器人的今日缘分（按日期确定性）
羁绊值由两人 QQ 号 + 当日日期做确定性哈希，相同输入每天结果稳定、不同天会变化。
"""
import re
import hashlib
from datetime import date

__plugin_meta__ = {
    "name": "关系查询",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "趣味计算两人羁绊值/CP 契合度，带关系标签",
    "priority": 50,
}

ctx = None

_LABELS_HIGH = ["天作之合", "灵魂伴侣", "天生一对", "命中注定", "如胶似漆"]
_LABELS_MID = ["颇有好感", "默契搭档", "欢喜冤家", "渐入佳境", "最佳拍档"]
_LABELS_LOW = ["点头之交", "塑料情谊", "欢喜冤家", "尚需磨合", "友谊小船"]


def _score(a, b, salt):
    key = f"{min(a,b)}-{max(a,b)}-{salt}"
    h = hashlib.md5(key.encode()).hexdigest()
    return int(h[:4], 16) % 101  # 0~100


def _nick(uid):
    try:
        info = ctx.get_member_info(0, uid) or {}
        return info.get("card") or info.get("nickname") or str(uid)
    except Exception:
        return str(uid)


def _targets(event, text):
    uids = []
    for u in (getattr(event, "at_list", None) or []):
        if str(u).isdigit():
            uids.append(int(u))
    for m in re.findall(r"(\d{5,})", text or ""):
        uids.append(int(m))
    return uids


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def handle_cp(event, match):
    """cp @A @B / 关系 <QQ1> <QQ2>"""
    text = (match.group(1) if match else "") or (event.message or "")
    uids = _targets(event, text)
    if len(uids) < 2:
        await _reply(event, "用法: /cp @用户A @用户B  或  /关系 <QQ1> <QQ2>")
        return
    a, b = uids[0], uids[1]
    today = date.today().isoformat()
    score = _score(a, b, today)
    if score >= 80:
        label = _LABELS_HIGH[score % len(_LABELS_HIGH)]
    elif score >= 50:
        label = _LABELS_MID[score % len(_LABELS_MID)]
    else:
        label = _LABELS_LOW[score % len(_LABELS_LOW)]
    na, nb = _nick(a), _nick(b)
    bar = "█" * (score // 5) + "░" * (20 - score // 5)
    await _reply(event,
        f"💞 {na} × {nb}\n"
        f"羁绊值：{score}/100\n"
        f"[{bar}]\n"
        f"关系标签：{label}")


async def handle_daily(event, match):
    """今日缘分 @用户"""
    text = (match.group(1) if match else "") or (event.message or "")
    uids = _targets(event, text)
    target = uids[0] if uids else event.user_id
    today = date.today().isoformat()
    score = _score(event.user_id, target, "daily-" + today)
    await _reply(event,
        f"🍀 今日缘分\n你（{_nick(event.user_id)}）与 {_nick(target)} 的今日缘分值：{score}/100")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("^/cp\\s*([\\s\\S]*)", handle_cp, priority=50, description="计算两人羁绊值")
    ctx.command("^/关系\\s*([\\s\\S]*)", handle_cp, priority=50, description="计算两人关系")
    ctx.command("^/今日缘分\\s*([\\s\\S]*)", handle_daily, priority=50, description="今日缘分值")
    ctx.logger.info("关系查询插件注册完成")

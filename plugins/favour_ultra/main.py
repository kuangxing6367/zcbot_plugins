"""
好感度系统插件 (favour_ultra)
=============================
  /好感度 [@用户]   -> 查看自己或他人与机器人的好感度
  /签到             -> 每日签到随机增加好感度（每天一次，0 点重置）
  /好感排行         -> 全服好感度 Top10
  /送好感 @用户 <n> -> 向他人赠送好感度（消耗自己好感）
数据：favour(user_id, value, coin, last_sign_day)。
"""
import re
import time
import random

__plugin_meta__ = {
    "name": "好感度系统",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "用户与机器人的好感度：签到/赠送/排行",
    "priority": 50,
}

ctx = None

_CREATE = """
CREATE TABLE IF NOT EXISTS favour (
    user_id BIGINT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0,
    coin INTEGER NOT NULL DEFAULT 0,
    last_sign_day VARCHAR(16) DEFAULT ''
)
"""


def _today():
    return time.strftime("%Y-%m-%d")


def _get(uid):
    rows = ctx.db_query("SELECT * FROM favour WHERE user_id=%s", [uid])
    if rows:
        return dict(rows[0])
    return {"user_id": uid, "value": 0, "coin": 0, "last_sign_day": ""}


def _save(row):
    ctx.db_execute(
        "INSERT INTO favour (user_id, value, coin, last_sign_day) VALUES (%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE value=%s, coin=%s, last_sign_day=%s",
        [row["user_id"], row["value"], row["coin"], row["last_sign_day"],
         row["value"], row["coin"], row["last_sign_day"]],
    )
    try:
        ctx.db_execute(
            "INSERT OR REPLACE INTO favour (user_id, value, coin, last_sign_day) VALUES (%s,%s,%s,%s)",
            [row["user_id"], row["value"], row["coin"], row["last_sign_day"]],
        )
    except Exception:
        pass


def _targets(event, text):
    uids = [int(u) for u in (getattr(event, "at_list", None) or []) if str(u).isdigit()]
    for m in re.findall(r"(\d{5,})", text or ""):
        uids.append(int(m))
    return uids


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def handle_view(event, match):
    """好感度 [@用户]"""
    uids = _targets(event, match.group(1) if match else "") if match else []
    uid = uids[0] if uids else event.user_id
    row = _get(uid)
    await _reply(event, f"💗 {uid} 与机器人的好感度：{row['value']}（硬币 {row['coin']}）")


async def handle_sign(event, match):
    """签到"""
    uid = event.user_id
    row = _get(uid)
    if row["last_sign_day"] == _today():
        await _reply(event, f"你今天已经签到过啦，当前好感度 {row['value']}（明天再来~）")
        return
    gain = random.randint(1, 5)
    row["value"] += gain
    row["coin"] += 1
    row["last_sign_day"] = _today()
    _save(row)
    await _reply(event, f"✅ 签到成功！好感度 +{gain}，当前 {row['value']}（得硬币 1）")


async def handle_rank(event, match):
    """好感排行"""
    rows = ctx.db_query("SELECT user_id, value FROM favour ORDER BY value DESC LIMIT 10")
    if not rows:
        await _reply(event, "还没有人积累好感度哦~")
        return
    lines = ["💗 好感度排行："]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['user_id']} — {r['value']}")
    await _reply(event, "\n".join(lines))


async def handle_give(event, match):
    """送好感 @用户 <n>"""
    text = (match.group(1) if match else "").strip()
    uids = _targets(event, text)
    if len(uids) < 1:
        await _reply(event, "用法: /送好感 @用户 <数量>")
        return
    m = re.search(r"(\d+)", text)
    amount = int(m.group(1)) if m else 1
    target = uids[0]
    if target == event.user_id:
        await _reply(event, "不能给自己送好感哦")
        return
    me = _get(event.user_id)
    if me["value"] < amount:
        await _reply(event, f"你的好感度不足（当前 {me['value']}）")
        return
    me["value"] -= amount
    _save(me)
    tgt = _get(target)
    tgt["value"] += amount
    _save(tgt)
    await _reply(event, f"已向 {target} 赠送 {amount} 点好感度！")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.db_execute(_CREATE, [])
    ctx.command("^/好感度\\s*([\\s\\S]*)$", handle_view, priority=50, description="查看好感度")
    ctx.command("^/签到\\s*$", handle_sign, priority=50, description="每日签到加好感")
    ctx.command("^/好感排行\\s*$", handle_rank, priority=50, description="好感度排行")
    ctx.command("^/送好感\\s+([\\s\\S]+)", handle_give, priority=50, description="赠送好感度")
    ctx.logger.info("好感度系统插件注册完成")

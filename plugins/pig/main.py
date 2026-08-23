"""
养猪小游戏插件 (pig)
=====================
功能（从 AstrBot「养小猪」类插件迁移，功能逻辑重写）：
  养猪      -> 认领一只小猪（每人仅一只）
  喂猪 <n>  -> 投喂，增加饱食度/体重，消耗金币
  猪状态    -> 查看自己小猪的状态（体重/饱食/心情/价值）
  遛猪      -> 散步增加心情
  杀猪      -> 屠宰换取金币（清空小猪）
  猪排行    -> 全服体重排行榜
数据持久化到插件表 pig_pets(user_id, weight, full, mood, coin, updated_at)。
"""
import time
import random

__plugin_meta__ = {
    "name": "养猪小游戏",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "群内养猪小游戏：认领/投喂/遛弯/屠宰换金币，带全服排行榜",
    "priority": 50,
}

ctx = None

_CREATE = """
CREATE TABLE IF NOT EXISTS pig_pets (
    user_id BIGINT PRIMARY KEY,
    weight REAL NOT NULL DEFAULT 5.0,
    full REAL NOT NULL DEFAULT 80.0,
    mood REAL NOT NULL DEFAULT 80.0,
    coin INTEGER NOT NULL DEFAULT 20,
    born_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
)
"""

_PRICES = [  # 饲料档位：花费金币 -> 增加体重
    (5, 0.5), (15, 1.5), (30, 3.2), (60, 7.0),
]
_NICKS = ["猪猪", "小香猪", "佩奇", "麦兜", "哼哼", "二师兄", "胖达猪"]


def _cfg(key, default):
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


def _load_int(key, default):
    try:
        return int(_cfg(key, default))
    except Exception:
        return default


def _now():
    return int(time.time())


def _decay(row):
    """随时间衰减饱食/心情（每 6 分钟掉 1 点），返回是否变更"""
    now = _now()
    dt = max(0, now - (row.get("updated_at") or now))
    drop = dt / 360.0  # 每小时约掉 0.17
    changed = False
    if row["full"] > 0:
        row["full"] = max(0.0, row["full"] - drop)
        changed = True
    if row["mood"] > 0:
        row["mood"] = max(0.0, row["mood"] - drop * 0.6)
        changed = True
    return changed


def _get(uid):
    try:
        rows = ctx.db_query("SELECT * FROM pig_pets WHERE user_id=%s", [uid])
        if not rows:
            return None
        row = dict(rows[0])
        if _decay(row):
            ctx.db_execute(
                "UPDATE pig_pets SET full=%s, mood=%s, updated_at=%s WHERE user_id=%s",
                [round(row["full"], 2), round(row["mood"], 2), _now(), uid],
            )
        return row
    except Exception as e:
        ctx.logger.warning(f"查猪失败: {e}")
        return None


def _save(row):
    ctx.db_execute(
        "UPDATE pig_pets SET weight=%s, full=%s, mood=%s, coin=%s, updated_at=%s WHERE user_id=%s",
        [round(row["weight"], 2), round(row["full"], 2), round(row["mood"], 2),
         int(row["coin"]), _now(), row["user_id"]],
    )


def _nick(uid):
    try:
        info = ctx.get_member_info(0, uid) or {}
        return info.get("card") or info.get("nickname") or str(uid)
    except Exception:
        return str(uid)


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


# ===================== 命令 =====================

async def handle_adopt(event, match):
    """养猪 -> 认领小猪"""
    uid = event.user_id
    if _get(uid):
        await _reply(event, "你已经有一只小猪啦，好好照顾它~")
        return
    row = {
        "user_id": uid, "weight": 5.0, "full": 80.0,
        "mood": 80.0, "coin": 20, "born_at": _now(), "updated_at": _now(),
    }
    try:
        ctx.db_execute(
            "INSERT INTO pig_pets (user_id, weight, full, mood, coin, born_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [uid, row["weight"], row["full"], row["mood"], row["coin"], row["born_at"], _now()],
        )
    except Exception as e:
        await _reply(event, f"认领失败: {e}")
        return
    await _reply(event, f"🐷 恭喜你认领了一只 {random.choice(_NICKS)}！初始体重 5.0kg，记得喂它~")


async def handle_feed(event, match):
    """喂猪 <档位> -> 投喂"""
    uid = event.user_id
    row = _get(uid)
    if not row:
        await _reply(event, "你还没有小猪，先发送「养猪」认领一只吧~")
        return
    text = (match.group(1) or "").strip() if match else ""
    idx = 0
    if text.isdigit():
        idx = max(0, min(len(_PRICES) - 1, int(text) - 1))
    cost, gain = _PRICES[idx]
    if row["coin"] < cost:
        await _reply(event, f"金币不足，该饲料需 {cost} 金币（你有 {int(row['coin'])}）")
        return
    row["coin"] -= cost
    row["weight"] = round(row["weight"] + gain, 2)
    row["full"] = min(100.0, row["full"] + 15.0)
    row["mood"] = min(100.0, row["mood"] + 5.0)
    _save(row)
    await _reply(event, f"🍞 投喂成功！小猪体重 {row['weight']}kg（+{gain}），饱食度 {int(row['full'])}%")


async def handle_status(event, match):
    """猪状态 -> 查看小猪"""
    uid = event.user_id
    row = _get(uid)
    if not row:
        await _reply(event, "你还没有小猪，先发送「养猪」认领一只吧~")
        return
    w = row["weight"]
    if w < 10:
        fig = "🐭 瘦弱小猪"
    elif w < 50:
        fig = "🐷 健壮小猪"
    elif w < 120:
        fig = "🐗 胖乎乎大猪"
    else:
        fig = "🐲 猪中巨兽"
    await _reply(event,
        f"【{_nick(uid)} 的小猪】\n"
        f"{fig}\n体重: {w} kg\n饱食度: {int(row['full'])}%\n"
        f"心情: {int(row['mood'])}%\n金币: {int(row['coin'])}")


async def handle_walk(event, match):
    """遛猪 -> 散步加心情"""
    uid = event.user_id
    row = _get(uid)
    if not row:
        await _reply(event, "你还没有小猪~")
        return
    gain = random.randint(3, 10)
    row["mood"] = min(100.0, row["mood"] + gain)
    row["full"] = max(0.0, row["full"] - 5.0)
    _save(row)
    await _reply(event, f"🚶 带着小猪散步了一圈，心情 +{gain}（{int(row['mood'])}%）")


async def handle_slaughter(event, match):
    """杀猪 -> 屠宰换金币"""
    uid = event.user_id
    row = _get(uid)
    if not row:
        await _reply(event, "你还没有小猪~")
        return
    price = _load_int("price_per_kg", 2)
    earn = int(row["weight"] * price)
    try:
        ctx.db_execute("DELETE FROM pig_pets WHERE user_id=%s", [uid])
    except Exception as e:
        await _reply(event, f"屠宰失败: {e}")
        return
    # 金币可累计到用户账户（若框架有 coins 表则累加，否则仅提示）
    await _reply(event, f"🔪 小猪被屠宰，换来 {earn} 金币！（体重 {row['weight']}kg × {price}/kg）")


async def handle_rank(event, match):
    """猪排行 -> 全服体重榜"""
    try:
        rows = ctx.db_query(
            "SELECT user_id, weight FROM pig_pets ORDER BY weight DESC LIMIT 10"
        )
    except Exception as e:
        await _reply(event, f"查询排行失败: {e}")
        return
    if not rows:
        await _reply(event, "还没有人养猪呢，快来认领第一只小猪！")
        return
    lines = ["🏆 养猪排行榜（按体重）"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {_nick(int(r['user_id']))} — {r['weight']}kg")
    await _reply(event, "\n".join(lines))


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.db_execute(_CREATE, [])
    ctx.command("^养猪\\s*$", handle_adopt, priority=50, description="认领一只小猪")
    ctx.command("^喂猪\\s*(\\d*)$", handle_feed, priority=50, description="投喂小猪（可带档位1-4）")
    ctx.command("^猪状态\\s*$", handle_status, priority=50, description="查看小猪状态")
    ctx.command("^遛猪\\s*$", handle_walk, priority=50, description="带小猪散步")
    ctx.command("^杀猪\\s*$", handle_slaughter, priority=50, description="屠宰小猪换金币")
    ctx.command("^猪排行\\s*$", handle_rank, priority=50, description="全服养猪排行榜")
    ctx.logger.info("养猪插件注册完成")

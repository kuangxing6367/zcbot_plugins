"""
赞我插件 - 点赞 & 每日自动点赞
================================
命令：
  /赞我 /点赞我 /赞我啊 /like /zan      给自己点赞
  /点赞 /赞 /点赞呢                    给指定 QQ 点赞（可带 QQ 号或 @）
  /自动点赞 /注册点赞 /开启自动点赞    开启每日自动点赞
  /取消自动点赞 /关闭自动点赞          取消每日自动点赞
  /点赞状态 /我的点赞                  查看点赞状态
  /点赞帮助 /这是什么                  查看使用帮助

点赞规则：
  - 普通用户：每次随机 0~8 个赞（可配置 min_times / max_times）
  - 超级管理员：固定 10 个赞（可配置 super_times），每天仅可点一次（"每天十个赞"，0 点重置）
  - 自动点赞：注册用户每天定时（默认 00:05）自动点赞，普通用户 0~8 随机，超管固定 10

可配置项（ctx.get_config 读取，均有默认值兜底）：
  min_times / max_times / super_times / super_daily_once /
  cooldown_seconds / auto_cron / auto_notify
"""
import asyncio
import random
import time
from datetime import datetime

__plugin_meta__ = {
    "name": "赞我",
    "version": "3.0.0",
    "author": "ZGRIC",
    "desc": "点赞插件：普通用户0~8随机赞，超管每天固定10赞，支持自动点赞",
    "priority": 50,
}

# ctx 由框架注入到模块全局变量
ctx = None

# 普通用户点赞冷却记录：user_id -> 上次点赞时间戳
_cooldown = {}
# 冷却记录上限：超出后清理过期条目，防长期运行内存无限增长
_COOLDOWN_MAX = 5000
_cooldown_checks = 0

_CREATE_AUTO_TABLE = """
CREATE TABLE IF NOT EXISTS send_like_auto (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id BIGINT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id)
)
"""

_CREATE_DAILY_TABLE = """
CREATE TABLE IF NOT EXISTS send_like_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id BIGINT NOT NULL,
    day VARCHAR(16) NOT NULL,
    UNIQUE(user_id, day)
)
"""

HELP_TEXT = """📌 点赞插件使用说明
━━━━━━━━━━━━━━
▸ /赞我  /点赞我  /赞我啊  → 给自己点赞
▸ /点赞 123  /赞 @某人    → 给指定目标点赞
▸ /自动点赞  /注册点赞    → 开启每日自动点赞
▸ /取消自动点赞           → 关闭每日自动点赞
▸ /点赞状态               → 查看当前状态
━━━━━━━━━━━━━━
🎯 规则：普通用户每次随机 0~8 个赞
👑 超级管理员每天固定 10 个赞"""


# ===================== 工具函数 =====================

def _get_conf(key, default):
    """读取配置项，异常时回退默认值"""
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


def _load_int(key, default, min_value=0):
    """读取整数配置，非法值回退默认值，并保证不小于 min_value"""
    try:
        value = int(_get_conf(key, default))
    except (TypeError, ValueError):
        value = default
    return max(min_value, value)


def _load_str(key, default):
    """读取字符串配置"""
    try:
        return str(_get_conf(key, default)).strip()
    except Exception:
        return default


def _load_bool(key, default):
    """读取布尔配置，兼容 bool / 字符串表示"""
    try:
        value = _get_conf(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return default


def _log(msg, level="warning"):
    """统一日志输出"""
    try:
        getattr(ctx.logger, level)(f"[赞我] {msg}")
    except Exception:
        pass


def _today_str():
    """今天的日期字符串 YYYY-MM-DD"""
    return datetime.now().strftime("%Y-%m-%d")


# ===================== 数据库 =====================

def _init_tables():
    """初始化数据表（自动适配 SQLite / MySQL）"""
    for sql in (_CREATE_AUTO_TABLE, _CREATE_DAILY_TABLE):
        try:
            ctx.db_execute(sql, [])
        except Exception as e:
            _log(f"建表失败: {e}")


def _is_superuser_by_id(user_id):
    """根据 QQ 号判断是否超级管理员（定时任务无 event 对象时使用）"""
    try:
        rows = ctx.db_query(
            "SELECT role FROM users WHERE user_id=%s", [user_id]
        )
        if rows:
            return rows[0].get("role") == "super"
    except Exception as e:
        _log(f"查询用户角色失败 user_id={user_id}: {e}")
    return False


def _super_used_today(user_id):
    """超级管理员今天是否已使用过手动点赞"""
    try:
        rows = ctx.db_query(
            "SELECT id FROM send_like_daily WHERE user_id=%s AND day=%s",
            [user_id, _today_str()],
        )
        return bool(rows)
    except Exception as e:
        _log(f"查询超管每日点赞记录失败: {e}")
        return False


def _mark_super_used_today(user_id):
    """记录超级管理员今天的点赞"""
    try:
        ctx.db_execute(
            "INSERT INTO send_like_daily (user_id, day) VALUES (%s, %s)",
            [user_id, _today_str()],
        )
    except Exception as e:
        _log(f"记录超管每日点赞失败: {e}")


def _is_auto_registered(user_id):
    """是否已注册每日自动点赞"""
    try:
        rows = ctx.db_query(
            "SELECT user_id FROM send_like_auto WHERE user_id=%s", [user_id]
        )
        return bool(rows)
    except Exception:
        return False


def _register_auto(user_id):
    """注册每日自动点赞：成功返回 True，已注册返回 False"""
    if _is_auto_registered(user_id):
        return False
    try:
        ctx.db_execute(
            "INSERT INTO send_like_auto (user_id, created_at) VALUES (%s, %s)",
            [user_id, int(time.time())],
        )
        return True
    except Exception as e:
        _log(f"注册自动点赞失败: {e}")
        return False


def _cancel_auto(user_id):
    """取消每日自动点赞"""
    try:
        ctx.db_execute("DELETE FROM send_like_auto WHERE user_id=%s", [user_id])
        return True
    except Exception:
        return False


# ===================== 点赞逻辑 =====================

def _parse_times(event):
    """计算点赞次数：普通用户 0~8 随机，超管固定 super_times（默认 10）"""
    if getattr(event, "is_superuser", False):
        return _load_int("super_times", 10, min_value=1)
    min_times = _load_int("min_times", 0, min_value=0)
    max_times = _load_int("max_times", 8, min_value=min_times)
    max_times = max(max_times, min_times)
    return random.randint(min_times, max_times)


def _resolve_target(event, match):
    """解析点赞目标：优先 @，其次纯数字参数，最后是发送者自己"""
    for uid in (getattr(event, "at_list", None) or []):
        if uid and str(uid).isdigit():
            return int(uid)
    text = ""
    if match:
        text = (match.group(1) or "").strip()
    if text and text.isdigit():
        return int(text)
    return event.user_id


def _check_cooldown(user_id):
    """普通用户冷却检查：通过返回 None，未通过返回剩余秒数"""
    cooldown_seconds = _load_int("cooldown_seconds", 30, min_value=0)
    if cooldown_seconds <= 0:
        return None
    now = time.time()
    last = _cooldown.get(user_id, 0)
    remain = int(cooldown_seconds - (now - last))
    if remain > 0:
        return remain
    _cooldown[user_id] = now
    # 惰性上限清理：每 128 次检查一次规模，超限剔除冷却已结束的条目
    global _cooldown_checks
    _cooldown_checks += 1
    if _cooldown_checks % 128 == 0 and len(_cooldown) > _COOLDOWN_MAX:
        expired = [u for u, t in _cooldown.items() if now - t >= cooldown_seconds]
        for u in expired:
            _cooldown.pop(u, None)
    return None


def _split_times(times, chunk=10):
    """将总点赞数拆分为多次调用（每次最多 10 个，适配接口单次上限）"""
    parts = []
    while times > 0:
        take = min(chunk, times)
        parts.append(take)
        times -= take
    return parts


async def _do_like(target, times):
    """分批调用 send_like 接口，返回成功点赞数"""
    done = 0
    for chunk in _split_times(times):
        try:
            await ctx.aapi("send_like", user_id=target, times=chunk)
            done += chunk
        except Exception as e:
            _log(f"点赞失败 user_id={target} chunk={chunk}: {e}")
            break
        try:
            await asyncio.sleep(0.3)
        except Exception:
            break
    return done


async def _reply(event, message):
    """统一回复：群聊回群、私聊回私"""
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=message,
    )


# ===================== 命令处理器 =====================

async def handle_like(event, match):
    """点赞命令：/赞我、/点赞、/赞 等"""
    target = _resolve_target(event, match)
    uid = event.user_id
    who = "你" if target == uid else f"TA({target})"

    # —— 超级管理员：固定 10 个赞，每天仅一次（"每天十个赞"）——
    if getattr(event, "is_superuser", False):
        if _load_bool("super_daily_once", True) and _super_used_today(uid):
            await _reply(event, "👑 超级管理员每天固定 10 个赞，今天已经点过啦，明天再来~")
            return
        times = _parse_times(event)
        success = await _do_like(target, times)
        if success <= 0:
            await _reply(event, "点赞失败，请稍后再试")
            return
        if _load_bool("super_daily_once", True):
            _mark_super_used_today(uid)
        await _reply(event, f"👑 超级管理员专属：已为{who}点满 {success} 个赞（每天固定 10 赞）")
        return

    # —— 普通用户：冷却 + 0~8 随机 ——
    cooldown_left = _check_cooldown(uid)
    if cooldown_left is not None:
        await _reply(event, f"操作太频繁了，请 {cooldown_left} 秒后再试")
        return
    times = _parse_times(event)
    success = await _do_like(target, times)
    if success <= 0:
        await _reply(event, "这次随机到 0 个赞（普通用户每次 0~8 随机），再试试手气吧~")
        return
    await _reply(event, f"已为{who}点了 {success} 个赞（普通用户每次随机 0~8 个）")


async def handle_auto_on(event, match):
    """开启每日自动点赞"""
    uid = event.user_id
    if _register_auto(uid):
        await _reply(event, "✅ 已开启每日自动点赞！每天会自动为你点赞（普通用户 0~8 随机，超管固定 10）。发送 /取消自动点赞 可关闭。")
    else:
        await _reply(event, "你已注册过自动点赞啦，发送 /取消自动点赞 可关闭")


async def handle_auto_off(event, match):
    """取消每日自动点赞"""
    uid = event.user_id
    if _cancel_auto(uid):
        await _reply(event, "已取消每日自动点赞")
    else:
        await _reply(event, "你还没有开启自动点赞哦，发送 /自动点赞 可开启")


async def handle_status(event, match):
    """查看点赞状态"""
    uid = event.user_id
    auto = _is_auto_registered(uid)
    role = "超级管理员" if getattr(event, "is_superuser", False) else "普通用户"
    if getattr(event, "is_superuser", False):
        used = _super_used_today(uid)
        plan = "每天固定 10 个赞（今天已点）" if used else "每天固定 10 个赞（今天未点）"
    else:
        plan = "每次随机 0~8 个赞"
    await _reply(
        event,
        f"📊 你的点赞状态：\n身份：{role}\n点赞规则：{plan}\n每日自动点赞：{'已开启' if auto else '未开启'}",
    )


async def handle_help(event, match):
    """点赞插件使用帮助"""
    await _reply(event, HELP_TEXT)


# ===================== 定时任务 =====================

async def task_auto_like():
    """每日自动点赞：对已注册用户自动点赞（默认每天 00:05 执行）"""
    try:
        rows = ctx.db_query("SELECT user_id FROM send_like_auto")
    except Exception as e:
        _log(f"查询自动点赞用户列表失败: {e}")
        return
    if not rows:
        _log("自动点赞：暂无注册用户", "info")
        return

    notify = _load_bool("auto_notify", True)
    liked = 0
    failed = 0
    for row in rows:
        uid = row.get("user_id")
        if not uid:
            continue
        try:
            if _is_superuser_by_id(uid):
                times = _load_int("super_times", 10, min_value=1)
            else:
                min_times = _load_int("min_times", 0, min_value=0)
                max_times = max(min_times, _load_int("max_times", 8, min_value=0))
                times = random.randint(min_times, max_times)
            success = await _do_like(int(uid), times)
            if success > 0:
                liked += 1
                if notify:
                    try:
                        await ctx.asend_msg(
                            user_id=int(uid),
                            group_id=None,
                            message=f"⭐ 每日自动点赞完成！已为你点了 {success} 个赞",
                        )
                    except Exception as e:
                        _log(f"发送自动点赞通知失败 user_id={uid}: {e}")
            else:
                failed += 1
            _log(f"自动点赞 user_id={uid} success={success}", "info")
        except Exception as e:
            failed += 1
            _log(f"自动点赞异常 user_id={uid}: {e}")
    _log(f"自动点赞完成：成功 {liked} 人，失败 {failed} 人", "info")


# ===================== 注册入口 =====================

def register(ctx):
    """插件注册入口"""
    ctx.command("/赞我", handle_like, priority=50,
                alias=["/点赞我", "/赞我啊", "/like", "/zan", "/赞我呢"],
                description="给自己点赞")
    ctx.command("/点赞", handle_like, priority=50,
                alias=["/赞", "/点赞呢"],
                description="给指定 QQ 点赞，可带 QQ 号或 @，无参数时赞自己")
    ctx.command("/自动点赞", handle_auto_on, priority=50,
                alias=["/注册点赞", "/开启自动点赞", "/开通自动点赞"],
                description="开启每日自动点赞")
    ctx.command("/取消自动点赞", handle_auto_off, priority=50,
                alias=["/关闭自动点赞", "/取消赞"],
                description="取消每日自动点赞")
    ctx.command("/点赞状态", handle_status, priority=50,
                alias=["/我的点赞", "/自动点赞状态"],
                description="查看点赞状态")
    ctx.command("/点赞帮助", handle_help, priority=50,
                alias=["/这是什么", "/点赞是什么", "/赞我是什么"],
                description="点赞插件使用帮助")

    # 初始化数据库表
    _init_tables()

    # 每日自动点赞定时任务（默认每天 00:05，可用 auto_cron 配置调整）
    cron = _load_str("auto_cron", "5 0 * * *")
    ctx.task(cron, task_auto_like, description="每日自动点赞")

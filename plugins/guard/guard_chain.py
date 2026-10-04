# -*- coding: utf-8 -*-
"""防护中心 · 统一拦截链

一条 on_raw_message 走完全部防护环节。本插件 priority=3（由 __plugin_meta__
表达），晚于会话等待器（priority=1）执行，因此多轮会话里正在等待的输入
不会被拦截链吞掉：

    黑名单丢弃 → 群白名单 → 唤醒词 → 敏感词过滤 → 限流 → 刷屏检测

环节语义：
- 黑名单 / 群白名单 / 唤醒词 / 敏感词 / 限流：命中即静默丢弃（返回 True 接管）；
- 刷屏检测：旁路处置——命中后自动禁言并在群内提示，但消息本身继续参与
  命令匹配 / 关键词回复（刷屏的人也会发命令，不能因为检测而漏处理）；
- 各环节均有独立开关（Web 配置页），管理员/群主/超管默认豁免除「全局
  黑名单」外的全部丢弃环节（可用配置关闭豁免）。

状态说明：
- 配置 60 秒内存缓存，Web 面板改完最迟 60 秒生效；
- 全局黑名单 60 秒内存缓存，命令增删即时同步；
- 限流窗口与刷屏窗口为纯内存态，进程重启清零；定时任务周期清理防增长。
"""
import threading
import time
from collections import deque

import guard_store

ctx = None  # 框架上下文，由 main.register 注入

# ── 配置缓存（60 秒 TTL）──────────────────────────────────
_CFG_TTL = 60.0
_cfg_cache = {"t": 0.0, "data": None}

# ── 全局黑名单缓存（60 秒 TTL）────────────────────────────
_BLOCK_TTL = 60.0
_block_ids = set()
_block_t = 0.0

# ── 限流状态：sid(user:group) -> deque[时间戳]，固定窗口 ──
_rate_windows = {}
_RATE_MAX_SESSIONS = 5000

# ── 刷屏检测状态（纯内存，进程重启清零）──────────────────
_state_lock = threading.RLock()
_user_hits = {}           # (group_id, user_id) -> [时间戳, ...]（滑动窗口）
_group_hits = {}          # group_id -> [时间戳, ...]（滑动窗口）
_muted_until = {}         # (group_id, user_id) -> 解禁时间戳（去重，避免重复禁言）
_group_muted_until = {}   # group_id -> 全群解禁时间戳
_spam_cache = {}          # group_id -> (群配置, 过期时间戳)
_SETTINGS_TTL = 30.0
_STATE_MAX = 5000         # 各状态表条目上限，超限淘汰最早插入的条目

# 刷屏检测内置默认阈值（群级未配置时生效，可被插件配置覆盖）
SPAM_DEFAULTS = {
    "enabled": 1,           # 本群是否启用
    "user_threshold": 8,    # 单人窗口内消息条数上限
    "user_window": 5,       # 单人统计窗口（秒）
    "user_mute": 300,       # 单人命中禁言时长（秒）
    "group_threshold": 20,  # 整群窗口内消息条数上限
    "group_window": 5,      # 整群统计窗口（秒）
    "group_mute": 600,      # 全体禁言时长（秒）
    "exempt_admin": 1,      # 管理员/群主/超管是否豁免
    "notify": 1,            # 处置后是否在群里提示
}


def init(ctx_obj):
    """注入框架上下文（main.register 调用）"""
    global ctx
    ctx = ctx_obj


def clear_state():
    """卸载时清空全部内存状态"""
    _rate_windows.clear()
    _block_ids.clear()
    with _state_lock:
        _user_hits.clear()
        _group_hits.clear()
        _muted_until.clear()
        _group_muted_until.clear()
        _spam_cache.clear()


# ===================== 配置读取 =====================

def _get_config(key, default=None):
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


def _cfg_bool(key, default):
    v = _get_config(key, None)
    if v is None:
        return default
    return str(v).lower() in ("1", "true", "yes", "on")


def _cfg_int(key, default):
    try:
        return int(_get_config(key, default))
    except (TypeError, ValueError):
        return default


def _cfg_list(key):
    """逗号分隔配置项 → 字符串列表（兼容中文逗号与数组型配置）"""
    v = _get_config(key, None)
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
    return []


def _cfg_int_list(key):
    """逗号分隔配置项 → 整数列表"""
    out = []
    for x in _cfg_list(key):
        try:
            out.append(int(float(x)))
        except ValueError:
            continue
    return out


def get_config() -> dict:
    """读取拦截链全部配置项（60 秒缓存，避免每条消息查库）"""
    now = time.time()
    if _cfg_cache["data"] is None or now - _cfg_cache["t"] > _CFG_TTL:
        _cfg_cache["data"] = {
            "exempt_admin": _cfg_bool("exempt_admin", True),
            "block_users": _cfg_int_list("block_users"),
            "whitelist_enable": _cfg_bool("whitelist_enable", False),
            "whitelist_groups": _cfg_int_list("whitelist_groups"),
            "wake_enable": _cfg_bool("wake_enable", False),
            "wake_prefixes": _cfg_list("wake_prefixes"),
            "wake_private": _cfg_bool("wake_private", False),
            "sensitive_enable": _cfg_bool("sensitive_enable", False),
            "sensitive_words": _cfg_list("sensitive_words"),
            "rate_enable": _cfg_bool("rate_enable", False),
            "rate_count": _cfg_int("rate_count", 5),
            "rate_seconds": _cfg_int("rate_seconds", 10),
        }
        _cfg_cache["t"] = now
    return _cfg_cache["data"]


def spam_config_defaults() -> dict:
    """插件配置里的全局阈值 → 作为群级未配置时的默认值"""
    got = {}
    if _get_config("default_enabled") is not None:
        got["enabled"] = 1 if _cfg_bool("default_enabled", True) else 0
    for key in ("user_threshold", "user_window", "user_mute",
                "group_threshold", "group_window", "group_mute"):
        v = _get_config(key, None)
        if v is not None:
            try:
                got[key] = int(v)
            except (TypeError, ValueError):
                pass
    if _get_config("exempt_admin") is not None:
        got["exempt_admin"] = 1 if _cfg_bool("exempt_admin", True) else 0
    if _get_config("spam_notify") is not None:
        got["notify"] = 1 if _cfg_bool("spam_notify", True) else 0
    return got


# ===================== 全局黑名单缓存 =====================

async def ensure_block_ids(force: bool = False):
    """刷新全局黑名单缓存（默认 60 秒 TTL；force=True 立即重查）"""
    global _block_t
    now = time.time()
    if not force and now - _block_t < _BLOCK_TTL:
        return
    try:
        ids = await guard_store.load_block_ids()
        _block_ids.clear()
        _block_ids.update(ids)
        _block_t = now
    except Exception as e:
        try:
            ctx.log(f"刷新黑名单缓存失败: {e}", level="warning")
        except Exception:
            pass


def block_ids_add(uid):
    """命令加黑后即时同步缓存"""
    try:
        _block_ids.add(int(uid))
    except (TypeError, ValueError):
        pass


def block_ids_discard(uid):
    """命令解黑后即时同步缓存"""
    try:
        _block_ids.discard(int(uid))
    except (TypeError, ValueError):
        pass


def block_contains(uid) -> bool:
    """查询某 QQ 是否在黑名单缓存中"""
    try:
        return int(uid) in _block_ids
    except (TypeError, ValueError):
        return False


def block_count() -> int:
    """当前黑名单缓存人数（仪表盘卡片用）"""
    return len(_block_ids)


# ===================== 身份判断 =====================

_event_cls = None  # 框架 Event 类，惰性导入并缓存


def _role_of(raw: dict, bot_name: str) -> str:
    """查消息发送者完整身份（super/owner/admin/member/黑名单）。

    构造框架 Event 复用其身份归并逻辑，底层查询自带 60 秒缓存；
    查询失败返回空串（视为普通成员，不豁免）。
    """
    global _event_cls
    try:
        if _event_cls is None:
            from framework.messaging.event import Event
            _event_cls = Event
        ev = _event_cls(raw, bot_name)
        ev._framework = ctx._framework
        return ev.role or ""
    except Exception:
        return ""


# ===================== 环节实现 =====================

def _extract_text(raw: dict) -> str:
    """从原始事件提取纯文本（消息为段数组时拼接全部 text 段）"""
    msg = raw.get("message", "")
    if isinstance(msg, list):
        return "".join(
            s.get("data", {}).get("text", "")
            for s in msg if isinstance(s, dict) and s.get("type") == "text"
        )
    return str(msg or "")


def _is_wake(raw: dict, text: str, prefixes) -> bool:
    """是否命中唤醒条件：@机器人 或 消息以唤醒前缀开头"""
    self_id = str(raw.get("self_id", ""))
    msg = raw.get("message", "")
    if isinstance(msg, list):
        for s in msg:
            if (isinstance(s, dict) and s.get("type") == "at"
                    and str(s.get("data", {}).get("qq", "")) == self_id):
                return True
    for p in prefixes:
        if p and text.startswith(p):
            return True
    return False


def _rate_hit(sid: str, count: int, seconds: int) -> bool:
    """固定窗口限流：窗口内计数达到上限返回 True（应丢弃），否则计数放行"""
    if count <= 0 or seconds <= 0:
        return False
    if len(_rate_windows) > _RATE_MAX_SESSIONS:
        _cleanup_rate_windows()
    now = time.time()
    w = _rate_windows.setdefault(sid, deque())
    while w and now - w[0] > seconds:
        w.popleft()
    if len(w) >= count:
        return True
    w.append(now)
    return False


def _cleanup_rate_windows():
    """清掉 5 分钟无活动的限流窗口（防内存增长）"""
    now = time.time()
    for sid in [s for s, w in _rate_windows.items() if not w or now - w[-1] > 300]:
        _rate_windows.pop(sid, None)


# ===================== 刷屏检测 =====================

def _prune(ts_list, window):
    """滑动窗口裁剪：只保留窗口内的时间戳"""
    cutoff = time.time() - window
    return [t for t in ts_list if t >= cutoff]


def _trim_locked():
    """状态条目上限保护（调用前需持锁）：超限淘汰最早插入的条目"""
    for store in (_user_hits, _group_hits, _muted_until, _group_muted_until):
        over = len(store) - _STATE_MAX
        if over <= 0:
            continue
        for k in list(store)[:over]:
            store.pop(k, None)


async def get_spam_settings(group_id) -> dict:
    """读群级刷屏配置（30 秒缓存）。

    优先级：群级表配置 > 插件全局配置 > 内置默认。
    """
    now = time.time()
    cached = _spam_cache.get(group_id)
    if cached and cached[1] > now:
        return cached[0]
    settings = dict(SPAM_DEFAULTS)
    settings.update(spam_config_defaults())
    try:
        row = await ctx.db_query_one_async(
            "SELECT * FROM guard_spam_settings WHERE group_id=%s", (group_id,))
        if row:
            for k in SPAM_DEFAULTS:
                if row.get(k) is not None:
                    settings[k] = row[k]
    except Exception as e:
        try:
            ctx.log(f"读取群 {group_id} 刷屏配置失败: {e}", level="warning")
        except Exception:
            pass
    _spam_cache[group_id] = (settings, now + _SETTINGS_TTL)
    return settings


async def save_spam_settings(group_id, patch: dict) -> bool:
    """写群级刷屏配置（整行覆盖，delete+insert 兼容两种方言），成功后清缓存"""
    current = await get_spam_settings(group_id)
    current.update({k: v for k, v in patch.items() if v is not None})
    try:
        await ctx.db_execute_async(
            "DELETE FROM guard_spam_settings WHERE group_id=%s", (group_id,))
        await ctx.db_execute_async(
            "INSERT INTO guard_spam_settings (group_id, enabled, user_threshold, "
            "user_window, user_mute, group_threshold, group_window, group_mute, "
            "exempt_admin, notify) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (group_id, current["enabled"], current["user_threshold"],
             current["user_window"], current["user_mute"],
             current["group_threshold"], current["group_window"],
             current["group_mute"], current["exempt_admin"],
             current["notify"]))
    except Exception as e:
        try:
            ctx.log(f"写入群 {group_id} 刷屏配置失败: {e}", level="error")
        except Exception:
            pass
        return False
    _spam_cache.pop(group_id, None)
    return True


def clear_group_mute(gid):
    """清除群的「全体禁言中」去重标记（/spam 解禁 用）"""
    with _state_lock:
        _group_muted_until.pop(gid, None)


def spam_snapshot():
    """刷屏监控概况（仪表盘/总览用）：(监控群数, 全体禁言中的群数)"""
    now = time.time()
    with _state_lock:
        watching = len(_group_hits)
        muted = sum(1 for ts in _group_muted_until.values() if ts > now)
    return watching, muted


def _can_mute(bot_name=None) -> bool:
    """处置前确认接入端支持群管动作；不支持则只告警不动手，不假装成功"""
    try:
        services = ctx._framework.services
        adapter = services.adapter_for_source(bot_name) if bot_name else None
        if adapter is None:
            adapter = services.primary_adapter()
        if adapter is None:
            return False
        return (adapter.supports('group_admin', 'actions')
                or adapter.supports('group_admin'))
    except Exception:
        return False


async def _notify(group_id, text):
    """群内处置提示（发送失败只记日志，不影响处置）"""
    try:
        await ctx.asend_msg(group_id=group_id, message=text)
    except Exception as e:
        try:
            ctx.log(f"处置提示发送失败: {e}", level="debug")
        except Exception:
            pass


async def spam_check(raw: dict, bot_name: str):
    """刷屏检测（旁路处置）：单人/群体滑动窗口，命中自动禁言；永不接管消息"""
    if raw.get('message_type', '') != 'group':
        return
    gid = raw.get('group_id')
    uid = raw.get('user_id')
    if not gid or not uid:
        return

    s = await get_spam_settings(gid)
    if not s.get('enabled'):
        return
    # 群级管理员豁免（群配置可关）
    if s.get('exempt_admin') and _role_of(raw, bot_name) in ('super', 'owner', 'admin'):
        return

    now = time.time()
    ukey = (gid, uid)
    hit_user = hit_group = False
    with _state_lock:
        # 群体刷屏：整群窗口内消息数超限 → 全体禁言（禁言期间不重复触发）
        g_list = _prune(_group_hits.get(gid, []) + [now], s['group_window'])
        _group_hits[gid] = g_list
        if len(g_list) >= s['group_threshold'] \
                and _group_muted_until.get(gid, 0) < now:
            _group_muted_until[gid] = now + s['group_mute']
            _group_hits[gid] = []
            hit_group = True

        # 单人刷屏：同人窗口内消息数超限 → 禁言该人
        u_list = _prune(_user_hits.get(ukey, []) + [now], s['user_window'])
        _user_hits[ukey] = u_list
        if len(u_list) >= s['user_threshold'] \
                and _muted_until.get(ukey, 0) < now:
            _muted_until[ukey] = now + s['user_mute']
            _user_hits[ukey] = []
            hit_user = True

        _trim_locked()

    if hit_group:
        await _dispose_group(gid, s, bot_name)
    elif hit_user:
        await _dispose_user(gid, uid, s, bot_name)


async def _dispose_group(gid, s, bot_name=None):
    """群体刷屏处置：全体禁言 + 提示"""
    duration = int(s['group_mute'])
    if not _can_mute(bot_name):
        try:
            ctx.log(f"群 {gid} 触发群体刷屏，但接入端不支持全体禁言", level="warning")
        except Exception:
            pass
        if s.get('notify'):
            await _notify(gid, f"检测到刷屏（{s['group_window']} 秒内超过 "
                               f"{s['group_threshold']} 条），但当前接入端不支持"
                               f"全体禁言，请管理员手动处理。")
        return
    try:
        await ctx.amute_all(gid, True)
    except Exception as e:
        try:
            ctx.log(f"全体禁言失败(群 {gid}): {e}", level="error")
        except Exception:
            pass
        return
    if s.get('notify'):
        await _notify(gid, f"检测到刷屏，已开启全体禁言 {duration} 秒。")


async def _dispose_user(gid, uid, s, bot_name=None):
    """单人刷屏处置：禁言该用户 + 提示"""
    duration = int(s['user_mute'])
    if not _can_mute(bot_name):
        try:
            ctx.log(f"群 {gid} 用户 {uid} 刷屏，接入端不支持禁言", level="warning")
        except Exception:
            pass
        return
    try:
        await ctx.aban(gid, uid, duration)
    except Exception as e:
        try:
            ctx.log(f"禁言失败({gid}/{uid}): {e}", level="error")
        except Exception:
            pass
        return
    if s.get('notify'):
        await _notify(gid, f"{uid} 消息过于频繁，已禁言 {duration} 秒。")


# ===================== 拦截链入口 =====================

async def on_raw(raw: dict, bot_name: str):
    """统一拦截链入口（on_raw_message）。

    返回 True  → 消息被接管丢弃（框架跳过后续全部处理）；
    返回 False → 消息继续走命令匹配 / 关键词回复等正常流程。
    任何异常一律放行，绝不让防护本身把消息链路打断。
    """
    try:
        if raw.get('post_type') not in (None, '', 'message'):
            return False
        uid = raw.get('user_id') or 0
        gid = raw.get('group_id') or 0
        mtype = raw.get('message_type', '')
        text = _extract_text(raw)
        cfg = get_config()

        # ── 1. 黑名单（全局表）：命中即静默丢弃，不做身份豁免 ──
        if uid:
            await ensure_block_ids()
            try:
                uid_int = int(uid)
            except (TypeError, ValueError):
                uid_int = 0
            if uid_int and uid_int in _block_ids:
                try:
                    ctx.logger.info(f"黑名单用户 {uid} 的消息已丢弃")
                except Exception:
                    pass
                return True

        # ── 2. 管理员豁免：管理员/群主/超管跳过下述全部丢弃环节 ──
        exempt = False
        if cfg.get('exempt_admin', True) and uid:
            exempt = _role_of(raw, bot_name) in ('super', 'owner', 'admin')

        # ── 3. 配置屏蔽名单（逗号分隔 QQ 号，配置驱动）──
        if uid and not exempt:
            try:
                if int(uid) in cfg.get('block_users', []):
                    return True
            except (TypeError, ValueError):
                pass

        # ── 4. 群白名单：开启后不在名单内的群不响应 ──
        if mtype == 'group' and gid and cfg.get('whitelist_enable') \
                and not exempt:
            if gid not in cfg.get('whitelist_groups', []):
                return True

        # ── 5. 唤醒词：仅 @机器人 / 唤醒前缀开头的消息才响应 ──
        #    （空前缀不拦截，避免误开唤醒开关后消息全被丢弃）
        prefixes = cfg.get('wake_prefixes', [])
        if prefixes and not exempt:
            if mtype == 'group' and cfg.get('wake_enable'):
                if not _is_wake(raw, text, prefixes):
                    return True
            elif mtype == 'private' and cfg.get('wake_private'):
                if not _is_wake(raw, text, prefixes):
                    return True

        # ── 6. 敏感词过滤：包含即丢弃 ──
        if not exempt and cfg.get('sensitive_enable') and text:
            words = cfg.get('sensitive_words', [])
            if any(w and w in text for w in words):
                return True

        # ── 7. 限流：按「用户+群」固定窗口，超限丢弃 ──
        if not exempt and cfg.get('rate_enable') and uid:
            if _rate_hit(f"{uid}:{gid or 0}",
                         cfg.get('rate_count', 5),
                         cfg.get('rate_seconds', 10)):
                return True

        # ── 8. 刷屏检测：旁路处置（禁言+提示），消息继续走正常流程 ──
        await spam_check(raw, bot_name)
        return False
    except Exception as e:
        try:
            ctx.log(f"拦截链异常（放行）: {e}", level="error")
        except Exception:
            pass
        return False


# ===================== 定时清理 =====================

def cleanup():
    """定时任务：清理过期窗口与状态（cron 触发，不占消息热路径）"""
    now = time.time()
    # 限流窗口：5 分钟无活动即清
    for sid in [s for s, w in _rate_windows.items() if not w or now - w[-1] > 300]:
        _rate_windows.pop(sid, None)
    with _state_lock:
        # 刷屏窗口：10 分钟无活动即清
        for gid in [g for g, v in _group_hits.items() if not v or now - v[-1] > 600]:
            _group_hits.pop(gid, None)
        for k in [k for k, v in _user_hits.items() if not v or now - v[-1] > 600]:
            _user_hits.pop(k, None)
        # 已到期的禁言去重标记与过期群配置缓存
        for k in [k for k, v in _muted_until.items() if v <= now]:
            _muted_until.pop(k, None)
        for g in [g for g, v in _group_muted_until.items() if v <= now]:
            _group_muted_until.pop(g, None)
        for g in [g for g, (_, t) in _spam_cache.items() if t <= now]:
            _spam_cache.pop(g, None)

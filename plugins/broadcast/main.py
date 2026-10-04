# -*- coding: utf-8 -*-
"""
广播通知插件（broadcast）——超管广播通知套件
================================================
功能一览：

  通知发送
    /群广播 <内容>               向机器人所在的全部群发送同一条通知（超管）
    /群通知 <群号> <内容>         向指定群发送通知（管理员及以上）
    /好友通知 <QQ> <内容>         向指定好友发送通知（管理员及以上）
    /我的群                      列出机器人所在的全部群（超管）

  引用转发广播（超管）
    (引用消息)广播 [群聊|私聊|全部]   向广播名单内的目标转发被引用消息
    开启广播 [群聊|私聊] [序号]       把目标加入广播名单（缺省为当前群/当前用户）
    关闭广播 [群聊|私聊] [序号]       把目标移出广播名单
    广播列表 [群聊|私聊]              查看广播名单
    取消广播                         停止进行中的广播

  定时广播（超管）
    定时广播                       查看定时广播状态
    定时广播 开|关                 开启/关闭定时广播（命令级开关）
    定时广播 内容 <文本>            设置定时广播内容模板
    定时广播 cron <表达式>          设置定时广播触发时间（五段 cron）
    定时广播 测试                  立即试发一次定时广播
    定时广播 重置                  清除命令级设置，恢复跟随 Web UI 配置

  /广播帮助                       查看全部命令

实现要点：
  - 通知内容与被引用消息均支持文本/图片/文件等消息段：引用转发优先按消息段
    原样重发（get_msg 取回后过滤保留段），发送失败自动回退到单条转发动作
    （forward_group_single_msg / forward_friend_single_msg）。
  - 群发（手动广播与定时广播共用）在框架事件循环内的 asyncio 后台任务中执行，
    目标之间按配置的发送间隔错开（0~间隔上限随机），不阻塞消息处理；
    任务引用保存在 _runtime，插件卸载（on_unload）时统一取消。
  - 定时广播由框架定时任务（ctx.task，每分钟调度检查）驱动：到达配置的
    cron 触发点后，把群发协程提交为 asyncio 后台任务；修改 Web UI 配置后
    一分钟内生效，无需重载插件。
  - 运行状态（广播名单、定时广播命令级设置、上次触发时间）保存在
    ctx.get_data_dir() 下的 JSON 文件，不写入插件代码目录。
"""
import asyncio
import json
import os
import random
import re
from datetime import datetime

__plugin_meta__ = {
    "name": "广播通知",
    "version": "2.0.0",
    "author": "ZGRIC",
    "desc": "超管广播通知套件：全部/指定群与好友通知、引用消息转发（文本/图片/文件）、定时周期广播、发送间隔可配",
    "priority": 55,
}

ctx = None  # 框架注入的插件上下文（register 时赋值）

# 引用转发时保留的消息段类型（文本/图片/文件/表情/语音/视频）
_KEEP_SEG_TYPES = ("text", "image", "face", "record", "video", "file")

# 名单展示时每栏最多列出的条数（防止超长消息发送失败）
_LIST_LIMIT = 100

# 运行时任务引用：fanout_task 是当前群发的 asyncio 后台任务（框架事件循环内），
# fanout_cancel 是协作取消标志；插件卸载时通过 on_unload 统一清理
_runtime = {
    "fanout_task": None,
    "fanout_cancel": False,
}


# ======================== 基础工具 ========================


def _state_path():
    """运行状态文件路径（位于插件数据目录 plugins_dat/broadcast/）"""
    return os.path.join(ctx.get_data_dir(), "broadcast_state.json")


def _state_load():
    """读取运行状态（文件不存在或损坏时返回空字典）"""
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _state_save(state):
    """保存运行状态"""
    try:
        path = _state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        ctx.log(f"状态文件保存失败: {e}", "warning")


async def _reply(event, text):
    """在触发命令的会话内回复文本"""
    try:
        await ctx.asend_msg(
            user_id=event.user_id,
            group_id=event.group_id if getattr(event, "is_group", False) else None,
            message=text,
        )
    except Exception as e:
        ctx.log(f"回复失败: {e}", "warning")


def _parse_scope(word):
    """解析范围词：True=群聊 / False=好友 / 'all'=群+好友 / None=无法识别"""
    w = (word or "").strip().lower()
    if w in ("好友", "私聊", "friend", "f"):
        return False
    if w in ("群聊", "群", "group", "g"):
        return True
    if w in ("全部", "所有", "all", "a"):
        return "all"
    return None


def _parse_target_args(arg1, arg2):
    """
    解析「开启/关闭广播」的参数
    :return: (is_group, index, err)；index 为序号（1 起），None 表示当前群/当前用户
    """
    a1 = (arg1 or "").strip()
    a2 = (arg2 or "").strip()
    # 只有数字：按群聊序号处理
    if a1.isdigit() and not a2:
        idx = int(a1)
        if idx <= 0:
            return True, None, "序号必须大于 0"
        return True, idx, None
    if not a1:
        return True, None, None
    scope = _parse_scope(a1)
    if scope is None or scope == "all":
        return True, None, "参数错误，格式：开启广播 [群聊|私聊] [序号]"
    if not a2:
        return scope, None, None
    if not a2.isdigit():
        return scope, None, "序号必须是正整数"
    idx = int(a2)
    if idx <= 0:
        return scope, None, "序号必须大于 0"
    return scope, idx, None


def _id_sort_key(value):
    """ID 排序键：纯数字按数值排，其余按字符串排（保证列表与序号映射稳定一致）"""
    s = str(value)
    if s.isdigit():
        return (0, int(s), "")
    return (1, 0, s)


# ======================== 配置读取 ========================


def _interval_max():
    """发送间隔上限（秒）：相邻两个目标之间的实际间隔在 0~该值之间随机"""
    try:
        v = float(ctx.get_config("send_interval", 1.1))
    except (TypeError, ValueError):
        v = 1.1
    return max(0.0, v)


def _skip_source():
    """引用转发广播时是否跳过发出该消息的群/好友"""
    return bool(ctx.get_config("skip_source", True))


# ======================== OneBot 数据读取 ========================


async def _list_groups():
    """机器人所在的全部群（兼容完整回执 {'data': [...]} 与裸数组）"""
    try:
        res = await ctx.aapi("get_group_list")
    except Exception as e:
        ctx.log(f"获取群列表失败: {e}", "warning")
        return []
    data = res.get("data") if isinstance(res, dict) else res
    if isinstance(data, list):
        return [g for g in data if isinstance(g, dict)]
    return []


async def _list_friends():
    """机器人的全部好友"""
    try:
        res = await ctx.aapi("get_friend_list")
    except Exception as e:
        ctx.log(f"获取好友列表失败: {e}", "warning")
        return []
    data = res.get("data") if isinstance(res, dict) else res
    if isinstance(data, list):
        return [f for f in data if isinstance(f, dict)]
    return []


async def _target_by_index(is_group, index, event):
    """
    按范围与序号取目标信息
    :return: (target_id, name)；找不到返回 (None, None)
    """
    if is_group:
        groups = sorted(await _list_groups(), key=lambda g: _id_sort_key(g.get("group_id")))
        group = None
        if index:
            if 1 <= index <= len(groups):
                group = groups[index - 1]
        else:
            gid = str(getattr(event, "group_id", 0) or "")
            group = next((g for g in groups if str(g.get("group_id")) == gid), None)
        if group is None:
            return None, None
        return str(group.get("group_id")), group.get("group_name") or str(group.get("group_id"))
    friends = sorted(await _list_friends(), key=lambda f: _id_sort_key(f.get("user_id")))
    friend = None
    if index:
        if 1 <= index <= len(friends):
            friend = friends[index - 1]
    else:
        uid = str(getattr(event, "user_id", 0) or "")
        friend = next((f for f in friends if str(f.get("user_id")) == uid), None)
        if friend is None:
            return uid or None, uid or None
    if friend is None:
        return None, None
    name = friend.get("remark") or friend.get("nickname") or str(friend.get("user_id"))
    return str(friend.get("user_id")), name


# ======================== 广播名单（关闭名单）管理 ========================


def _disabled_key(is_group):
    return "disable_gids" if is_group else "disable_uids"


def _disabled_ids(is_group):
    """关闭名单：广播时跳过的群号/好友号列表"""
    raw = _state_load().get(_disabled_key(is_group), []) or []
    return [str(x) for x in raw]


def _set_target_enabled(target_id, is_group, enabled):
    """开启（移出关闭名单）/ 关闭（加入关闭名单）某目标的广播"""
    state = _state_load()
    key = _disabled_key(is_group)
    ids = [str(x) for x in state.get(key, []) or []]
    tid = str(target_id)
    if enabled and tid in ids:
        ids.remove(tid)
        state[key] = ids
        _state_save(state)
    elif not enabled and tid not in ids:
        ids.append(tid)
        state[key] = ids
        _state_save(state)


def _filter_enabled(target_ids, is_group):
    """从目标列表中剔除已关闭广播的 ID"""
    disabled = set(_disabled_ids(is_group))
    return [t for t in target_ids if str(t) not in disabled]


# ======================== 群发任务（asyncio 后台） ========================


def _fanout_busy():
    """是否有群发任务正在进行"""
    task = _runtime.get("fanout_task")
    return task is not None and not task.done()


def _spawn(coro):
    """
    把协程提交到框架事件循环后台执行（async 命令处理器与定时任务均在循环内，
    直接 create_task；兜底走 ctx.call_async 跨线程调度）
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        return loop.create_task(coro)
    return ctx.call_async(coro)


async def _fetch_quoted_segments(message_id):
    """
    拉取被引用消息的内容（文本/图片/文件等消息段）
    :return: 消息段列表 / CQ 字符串 / None（获取失败，由调用方回退转发动作）
    """
    try:
        res = await ctx.aapi("get_msg", message_id=message_id)
    except Exception as e:
        ctx.log(f"获取引用消息内容失败: {e}", "warning")
        return None
    data = res.get("data") if isinstance(res, dict) else None
    if not isinstance(data, dict):
        data = res if isinstance(res, dict) else {}
    msg = data.get("message")
    if isinstance(msg, str):
        return msg.strip() or None
    if isinstance(msg, list):
        segs = [s for s in msg
                if isinstance(s, dict) and s.get("type") in _KEEP_SEG_TYPES]
        return segs or None
    return None


def _as_int(value):
    """ID 尽量转 int（OneBot 参数要求），失败时原样返回"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


async def _send_one(target_id, is_group, payload, reply_msg_id):
    """
    向单个目标发送
    优先按消息段/文本直接发送；失败且有引用消息 ID 时，回退单条转发动作
    """
    try:
        if payload:
            if is_group:
                await ctx.aapi("send_group_msg",
                               group_id=_as_int(target_id), message=payload)
            else:
                await ctx.aapi("send_private_msg",
                               user_id=_as_int(target_id), message=payload)
            return True
    except Exception as e:
        ctx.log(f"目标 {target_id} 直接发送失败，尝试转发动作: {e}", "debug")
    if reply_msg_id:
        try:
            if is_group:
                await ctx.aapi("forward_group_single_msg",
                               group_id=_as_int(target_id), message_id=reply_msg_id)
            else:
                await ctx.aapi("forward_friend_single_msg",
                               user_id=_as_int(target_id), message_id=reply_msg_id)
            return True
        except Exception as e:
            ctx.log(f"目标 {target_id} 转发失败: {e}", "warning")
    return False


async def _fanout_job(targets, payload, reply_msg_id, scope_text, source):
    """
    群发主协程：在框架事件循环内按配置间隔逐个目标发送（asyncio 后台任务）
    :param targets: [(target_id, is_group), ...]
    :param payload: 文本字符串 / 消息段列表 / None（回退转发动作）
    :param reply_msg_id: 引用消息 ID（转发回退用，可为 None）
    :param scope_text: 范围描述（结果回报用）
    :param source: 触发会话 {'user_id', 'group_id'}；定时触发无来源传 None
    """
    total = len(targets)
    ok = 0
    fail = 0
    cancelled = False
    try:
        for i, (tid, is_group) in enumerate(targets):
            if _runtime["fanout_cancel"]:
                cancelled = True
                break
            if i > 0:
                interval = _interval_max()
                if interval > 0:
                    # 目标之间错开发送：0~间隔上限随机，防刷屏风控
                    await asyncio.sleep(random.uniform(0, interval))
            if await _send_one(tid, is_group, payload, reply_msg_id):
                ok += 1
            else:
                fail += 1
                ctx.log(f"广播目标 {tid} 发送失败", "warning")
    except asyncio.CancelledError:
        # 插件卸载等场景主动取消任务：停止群发并正常收尾
        cancelled = True
    finally:
        _runtime["fanout_task"] = None
    note = "（已取消）" if cancelled else ""
    if source:
        text = f"广播结束：成功 {ok}/{total} 个{scope_text}{note}"
        if fail:
            text += f"，失败 {fail} 个"
        try:
            await ctx.asend_msg(
                user_id=source.get("user_id"),
                group_id=source.get("group_id"),
                message=text,
            )
        except Exception as e:
            ctx.log(f"广播结果回报失败: {e}", "warning")
    else:
        ctx.log(f"定时广播结束：成功 {ok}/{total}{note}，失败 {fail}")


def _start_fanout(targets, payload, reply_msg_id, scope_text, source):
    """启动一次群发：登记任务引用后提交到事件循环，返回目标数"""
    _runtime["fanout_cancel"] = False
    task = _spawn(_fanout_job(targets, payload, reply_msg_id, scope_text, source))
    _runtime["fanout_task"] = task
    return len(targets)


# ======================== 定时广播 ========================


def _cron_valid(expr):
    """粗校验五段 cron 表达式格式"""
    parts = (expr or "").strip().split()
    if len(parts) != 5:
        return False
    return all(re.fullmatch(r"[\d*,\-/]+", p) for p in parts)


def _cron_field_hit(field, value, lo, hi):
    """cron 单字段匹配：支持 * 、*/步进 、区间 a-b 、区间步进 a-b/n 、枚举 a,b,c"""
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        body = part
        if "/" in part:
            body, _, step_s = part.partition("/")
            try:
                step = max(1, int(step_s))
            except ValueError:
                return False
        if body in ("*", ""):
            hit = lo <= value <= hi and (value - lo) % step == 0
        elif "-" in body:
            a, _, b = body.partition("-")
            try:
                lo_v, hi_v = int(a), int(b)
            except ValueError:
                return False
            hit = lo_v <= value <= hi_v and (value - lo_v) % step == 0
        else:
            try:
                v = int(body)
            except ValueError:
                return False
            hit = value == v if step == 1 else (v <= value <= hi and (value - v) % step == 0)
        if hit:
            return True
    return False


def _cron_restricted(field):
    """字段是否为受限值（非 * 形态）"""
    f = (field or "").strip()
    return f != "*" and not f.startswith("*/")


def _cron_match(expr, now):
    """
    轻量五段 cron 匹配（分 时 日 月 周），供每分钟的调度检查使用。
    标准语义：日与周同时受限时，满足其一即触发。
    """
    parts = (expr or "").strip().split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    if not _cron_field_hit(minute, now.minute, 0, 59):
        return False
    if not _cron_field_hit(hour, now.hour, 0, 23):
        return False
    if not _cron_field_hit(month, now.month, 1, 12):
        return False
    day_ok = _cron_field_hit(dom, now.day, 1, 31)
    dow_ok = _cron_field_hit(dow, (now.weekday() + 1) % 7, 0, 6)
    if _cron_restricted(dom) and _cron_restricted(dow):
        return day_ok or dow_ok
    return day_ok and dow_ok


def _render_template(text, now):
    """渲染定时广播内容模板占位符：{date} {time} {datetime}"""
    return (text
            .replace("{date}", now.strftime("%Y-%m-%d"))
            .replace("{time}", now.strftime("%H:%M"))
            .replace("{datetime}", now.strftime("%Y-%m-%d %H:%M")))


def _effective_schedule(state):
    """
    计算定时广播的生效配置（命令级设置优先于 Web UI 配置）
    :return: (enabled, cron_expr, content, scope, targets_cfg)
    """
    on = state.get("schedule_on")
    if on is None:
        on = bool(ctx.get_config("schedule_enabled", False))
    cron_expr = (str(state.get("schedule_cron") or "").strip()
                 or str(ctx.get_config("schedule_cron", "0 9 * * *") or "").strip())
    content = (str(state.get("schedule_content") or "").strip()
               or str(ctx.get_config("schedule_content", "") or "").strip())
    scope = str(ctx.get_config("schedule_scope", "group") or "group").strip().lower()
    if scope not in ("group", "friend", "all"):
        scope = "group"
    targets_cfg = ctx.get_config("schedule_targets", []) or []
    if isinstance(targets_cfg, str):
        targets_cfg = [x for x in re.split(r"[,\s，、]+", targets_cfg) if x.strip()]
    return bool(on), cron_expr, content, scope, [str(x) for x in targets_cfg]


async def _resolve_schedule_targets(scope, targets_cfg):
    """
    计算定时广播的目标列表（关闭名单始终生效）
    :param scope: group / friend / all（指定目标列表为空时使用）
    :param targets_cfg: 指定目标（群号或 QQ 号）列表；非空时仅向这些目标发送
    """
    groups = await _list_groups()
    friends = await _list_friends()
    dis_g = set(_disabled_ids(True))
    dis_u = set(_disabled_ids(False))
    result = []
    if targets_cfg:
        gmap = {str(g.get("group_id")) for g in groups if g.get("group_id")}
        fmap = {str(f.get("user_id")) for f in friends if f.get("user_id")}
        for tid in targets_cfg:
            if tid in gmap and tid not in dis_g:
                result.append((tid, True))
            elif tid in fmap and tid not in dis_u:
                result.append((tid, False))
        return result
    if scope in ("group", "all"):
        result += [(str(g.get("group_id")), True) for g in groups
                   if g.get("group_id") and str(g.get("group_id")) not in dis_g]
    if scope in ("friend", "all"):
        result += [(str(f.get("user_id")), False) for f in friends
                   if f.get("user_id") and str(f.get("user_id")) not in dis_u]
    return result


async def scheduled_broadcast_tick():
    """
    定时广播调度检查（框架定时任务，每分钟执行一次）：
    到达配置 cron 的触发分钟时，把群发协程提交为 asyncio 后台任务
    """
    try:
        await _schedule_tick()
    except Exception as e:
        try:
            ctx.log(f"定时广播调度异常: {e}", "warning")
        except Exception:
            pass


async def _schedule_tick():
    state = _state_load()
    enabled, cron_expr, content, scope, targets_cfg = _effective_schedule(state)
    if not enabled:
        return
    if not _cron_valid(cron_expr):
        return
    now = datetime.now()
    fire_key = now.strftime("%Y%m%d%H%M")
    # 同一分钟只触发一次（状态持久化，重载插件也不会重复发送）
    if state.get("last_fire_key") == fire_key:
        return
    if not _cron_match(cron_expr, now):
        return
    state["last_fire_key"] = fire_key
    _state_save(state)
    if not content:
        ctx.log("定时广播内容为空，本次跳过（请在 Web UI 配置或用「定时广播 内容」设置）", "warning")
        return
    if _fanout_busy():
        ctx.log("已有群发任务进行中，本次定时广播跳过", "warning")
        return
    targets = await _resolve_schedule_targets(scope, targets_cfg)
    if not targets:
        ctx.log("定时广播没有可用目标，本次跳过", "warning")
        return
    count = _start_fanout(targets, _render_template(content, now), None, "定时广播目标", None)
    ctx.log(f"定时广播已触发：{count} 个目标")


def _schedule_usage():
    return "子命令：定时广播 开|关 / 内容 <文本> / cron <五段表达式> / 测试 / 重置"


def _schedule_status_text(state):
    """「定时广播」状态文本"""
    on, cron_expr, content, scope, targets_cfg = _effective_schedule(state)
    if state.get("schedule_on") is None:
        on_desc = ("已开启" if on else "已关闭") + "（跟随 Web UI 配置）"
    else:
        on_desc = ("已开启" if on else "已关闭") + "（命令开关）"
    cron_desc = cron_expr or "（未设置）"
    if state.get("schedule_cron"):
        cron_desc += "（命令设置）"
    if targets_cfg:
        scope_desc = f"指定 {len(targets_cfg)} 个目标"
    else:
        scope_desc = {"group": "全部群", "friend": "全部好友", "all": "群聊+好友"}.get(scope, scope)
    if content:
        content_desc = content[:60] + ("…" if len(content) > 60 else "")
    else:
        content_desc = "（未设置）"
    last = str(state.get("last_fire_key") or "")
    if len(last) == 12:
        last_desc = f"{last[0:4]}-{last[4:6]}-{last[6:8]} {last[8:10]}:{last[10:12]}"
    else:
        last_desc = "尚无记录"
    return "\n".join([
        "【定时广播】",
        f"状态: {on_desc}",
        f"触发: {cron_desc}",
        f"范围: {scope_desc}",
        f"内容: {content_desc}",
        f"上次触发: {last_desc}",
        _schedule_usage(),
    ])


# ======================== 命令处理器：通知发送 ========================


async def handle_group_broadcast(event, match):
    """/群广播 <内容>：向全部群发送通知；有引用消息时转发被引用消息（支持图片/文件）"""
    content = (match.group(1) or "").strip() if match else ""
    reply_id = getattr(event, "reply_id", None)
    if reply_id:
        # 引用优先：转发被引用消息（文字参数忽略）
        payload = await _fetch_quoted_segments(reply_id)
    elif content:
        payload = content
    else:
        await _reply(event, "用法：/群广播 <内容>，或引用一条消息后发送 /群广播")
        return
    ids = [str(g.get("group_id")) for g in await _list_groups() if g.get("group_id")]
    if _skip_source() and event.is_group:
        ids = [i for i in ids if i != str(event.group_id)]
    if not ids:
        await _reply(event, "未获取到任何群，无法广播")
        return
    if _fanout_busy():
        await _reply(event, "已有广播正在进行中，请稍后再试或使用「取消广播」")
        return
    source = {"user_id": event.user_id,
              "group_id": event.group_id if event.is_group else None}
    count = _start_fanout([(i, True) for i in ids], payload, reply_id, "群聊", source)
    interval = _interval_max()
    tip = f"，发送间隔 0~{interval:g} 秒随机" if interval > 0 else ""
    await _reply(event, f"正在向 {count} 个群广播该消息{tip}，完成后回报结果...")


async def handle_group_notify(event, match):
    """/群通知 <群号> <内容>：向指定群发送通知；有引用消息时转发被引用消息"""
    text = (match.group(1) or "").strip() if match else ""
    m = re.match(r"^(\d{5,})\s*([\s\S]*)$", text)
    if not m:
        await _reply(event, "用法：/群通知 <群号> <内容>，或引用消息后发送 /群通知 <群号>")
        return
    gid = int(m.group(1))
    content = m.group(2).strip()
    reply_id = getattr(event, "reply_id", None)
    try:
        if reply_id:
            segs = await _fetch_quoted_segments(reply_id)
            if segs is None:
                await ctx.aapi("forward_group_single_msg",
                               group_id=gid, message_id=reply_id)
            else:
                await ctx.aapi("send_group_msg", group_id=gid, message=segs)
        elif content:
            await ctx.aapi("send_group_msg", group_id=gid, message=content)
        else:
            await _reply(event, "用法：/群通知 <群号> <内容>，或引用消息后发送 /群通知 <群号>")
            return
    except Exception as e:
        await _reply(event, f"发送失败: {e}")
        return
    await _reply(event, f"已向群 {gid} 发送通知")


async def handle_friend_notify(event, match):
    """/好友通知 <QQ> <内容>：向指定好友发送通知；有引用消息时转发被引用消息"""
    text = (match.group(1) or "").strip() if match else ""
    m = re.match(r"^(\d{5,})\s*([\s\S]*)$", text)
    if not m:
        await _reply(event, "用法：/好友通知 <QQ> <内容>，或引用消息后发送 /好友通知 <QQ>")
        return
    uid = int(m.group(1))
    content = m.group(2).strip()
    reply_id = getattr(event, "reply_id", None)
    try:
        if reply_id:
            segs = await _fetch_quoted_segments(reply_id)
            if segs is None:
                await ctx.aapi("forward_friend_single_msg",
                               user_id=uid, message_id=reply_id)
            else:
                await ctx.aapi("send_private_msg", user_id=uid, message=segs)
        elif content:
            await ctx.aapi("send_private_msg", user_id=uid, message=content)
        else:
            await _reply(event, "用法：/好友通知 <QQ> <内容>，或引用消息后发送 /好友通知 <QQ>")
            return
    except Exception as e:
        await _reply(event, f"发送失败: {e}")
        return
    await _reply(event, f"已向 {uid} 发送通知")


async def handle_my_groups(event, match):
    """/我的群：列出机器人所在的全部群（群号 + 群名）"""
    groups = sorted(await _list_groups(), key=lambda g: _id_sort_key(g.get("group_id")))
    if not groups:
        await _reply(event, "未获取到群列表")
        return
    lines = [f"机器人所在群（{len(groups)} 个）："]
    for g in groups[:_LIST_LIMIT // 2]:
        lines.append(f"  {g.get('group_name') or '?'} ({g.get('group_id')})")
    if len(groups) > _LIST_LIMIT // 2:
        lines.append(f"……（其余 {len(groups) - _LIST_LIMIT // 2} 个省略）")
    await _reply(event, "\n".join(lines))


# ======================== 命令处理器：引用转发广播 ========================


async def handle_broadcast_enable(event, match):
    """开启广播 [群聊|私聊] [序号]：把目标加入广播名单（缺省为当前群/当前用户）"""
    g1 = match.group(1) if match else None
    g2 = match.group(2) if match else None
    is_group, index, err = _parse_target_args(g1, g2)
    if err:
        await _reply(event, err)
        return
    tid, name = await _target_by_index(is_group, index, event)
    if not tid:
        await _reply(event, "未找到对应目标（序号越界或不在列表中），可先用「广播列表」查看序号")
        return
    _set_target_enabled(tid, is_group, True)
    await _reply(event, f"【{name}】已开启{'群聊' if is_group else '私聊'}广播")


async def handle_broadcast_disable(event, match):
    """关闭广播 [群聊|私聊] [序号]：把目标移出广播名单"""
    g1 = match.group(1) if match else None
    g2 = match.group(2) if match else None
    is_group, index, err = _parse_target_args(g1, g2)
    if err:
        await _reply(event, err)
        return
    tid, name = await _target_by_index(is_group, index, event)
    if not tid:
        await _reply(event, "未找到对应目标（序号越界或不在列表中），可先用「广播列表」查看序号")
        return
    _set_target_enabled(tid, is_group, False)
    await _reply(event, f"已关闭【{name}】的{'群聊' if is_group else '私聊'}广播")


async def handle_broadcast_list(event, match):
    """广播列表 [群聊|私聊]：按开启/关闭两栏展示广播名单"""
    arg = (match.group(1) or "").strip() if match else ""
    is_group = _parse_scope(arg) is not False  # 缺省或无法识别按群聊
    if is_group:
        label = "群聊"
        entries = sorted(await _list_groups(), key=lambda g: _id_sort_key(g.get("group_id")))
        items = [(str(g.get("group_id")),
                  f"{g.get('group_name') or g.get('group_id')} ({g.get('group_id')})")
                 for g in entries if g.get("group_id")]
    else:
        label = "好友"
        entries = sorted(await _list_friends(), key=lambda f: _id_sort_key(f.get("user_id")))
        items = []
        for f in entries:
            if not f.get("user_id"):
                continue
            uid = str(f.get("user_id"))
            name = f.get("remark") or f.get("nickname") or uid
            items.append((uid, f"{name} ({uid})"))
    disabled = set(_disabled_ids(is_group))
    enabled_lines = []
    disabled_lines = []
    for idx, (tid, info) in enumerate(items, 1):
        line = f"{idx}. {info}"
        (disabled_lines if tid in disabled else enabled_lines).append(line)

    def _clip(lines):
        if len(lines) <= _LIST_LIMIT:
            return lines
        return lines[:_LIST_LIMIT] + [f"……（其余 {len(lines) - _LIST_LIMIT} 个省略）"]

    parts = [f"【{label}·广播名单】（开启 {len(enabled_lines)} 个）"]
    parts += _clip(enabled_lines) or ["（空）"]
    if disabled_lines:
        parts.append(f"【{label}·已关闭广播】（{len(disabled_lines)} 个）")
        parts += _clip(disabled_lines)
    await _reply(event, "\n".join(parts))


async def handle_broadcast(event, match):
    """(引用消息)广播 [群聊|私聊|全部]：向广播名单内的目标转发被引用消息"""
    scope_arg = (match.group(1) or "").strip() if match else ""
    reply_id = getattr(event, "reply_id", None)
    if not reply_id:
        await _reply(event, "需要引用要广播的消息，用法：(引用消息)广播 [群聊|私聊|全部]")
        return
    scope = _parse_scope(scope_arg)
    if scope is None:
        scope = True  # 缺省按群聊处理
    if _fanout_busy():
        await _reply(event, "已有广播正在进行中，请稍后再试或使用「取消广播」")
        return
    payload = await _fetch_quoted_segments(reply_id)
    targets = []
    if scope is True or scope == "all":
        ids = _filter_enabled(
            [str(g.get("group_id")) for g in await _list_groups() if g.get("group_id")],
            True)
        if _skip_source() and event.is_group:
            ids = [i for i in ids if i != str(event.group_id)]
        targets += [(i, True) for i in ids]
    if scope is False or scope == "all":
        ids = _filter_enabled(
            [str(f.get("user_id")) for f in await _list_friends() if f.get("user_id")],
            False)
        if _skip_source() and not event.is_group:
            ids = [i for i in ids if i != str(event.user_id)]
        targets += [(i, False) for i in ids]
    if not targets:
        await _reply(event, "广播名单为空，没有可发送的目标（用「广播列表」查看、「开启广播」添加）")
        return
    scope_text = "群聊" if scope is True else ("好友" if scope is False else "群聊+好友")
    source = {"user_id": event.user_id,
              "group_id": event.group_id if event.is_group else None}
    count = _start_fanout(targets, payload, reply_id, scope_text, source)
    await _reply(event, f"正在向 {count} 个{scope_text}目标广播此消息，完成后回报结果...")


async def handle_broadcast_cancel(event, match):
    """取消当前正在进行的广播（协作取消：当前目标发完后停止）"""
    if not _fanout_busy():
        await _reply(event, "当前没有进行中的广播")
        return
    _runtime["fanout_cancel"] = True
    await _reply(event, "已请求取消广播，正在停止后续发送...")


# ======================== 命令处理器：定时广播 ========================


async def handle_schedule(event, match):
    """定时广播：查看状态与命令级设置"""
    arg = (match.group(1) or "").strip() if match else ""
    state = _state_load()
    if not arg:
        await _reply(event, _schedule_status_text(state))
        return
    head = arg.split(None, 1)
    sub = head[0]
    rest = head[1].strip() if len(head) > 1 else ""
    if sub in ("开", "开启", "on"):
        state["schedule_on"] = True
        _state_save(state)
        await _reply(event, "定时广播已开启（命令开关，优先于 Web UI 配置）")
    elif sub in ("关", "关闭", "off"):
        state["schedule_on"] = False
        _state_save(state)
        await _reply(event, "定时广播已关闭（命令开关，优先于 Web UI 配置）")
    elif sub == "重置":
        state["schedule_on"] = None
        state["schedule_content"] = ""
        state["schedule_cron"] = ""
        _state_save(state)
        await _reply(event, "已清除定时广播的命令级设置，恢复跟随 Web UI 配置")
    elif sub == "测试":
        await _schedule_test_fire(event, state)
    elif sub in ("内容", "模板"):
        if rest in ("", "清除", "清空"):
            state["schedule_content"] = ""
            _state_save(state)
            await _reply(event, "已清除定时广播内容，恢复跟随 Web UI 配置")
        else:
            state["schedule_content"] = rest
            _state_save(state)
            await _reply(event, f"定时广播内容已更新：{rest[:60]}{'…' if len(rest) > 60 else ''}")
    elif sub in ("cron", "时间", "周期"):
        expr = rest.strip()
        if not _cron_valid(expr):
            await _reply(event, "cron 表达式无效（需 5 段：分 时 日 月 周），"
                                "示例：定时广播 cron 0 9 * * *")
            return
        state["schedule_cron"] = expr
        _state_save(state)
        await _reply(event, f"定时广播触发时间已更新：{expr}")
    else:
        await _reply(event, _schedule_usage())


async def _schedule_test_fire(event, state):
    """「定时广播 测试」：按当前生效配置立即试发一次"""
    _, cron_expr, content, scope, targets_cfg = _effective_schedule(state)
    if not content:
        await _reply(event, "定时广播内容为空，请先「定时广播 内容 <文本>」或在 Web UI 配置")
        return
    if _fanout_busy():
        await _reply(event, "已有广播正在进行中，请稍后再试")
        return
    targets = await _resolve_schedule_targets(scope, targets_cfg)
    if not targets:
        await _reply(event, "没有可用的广播目标（请检查目标范围/指定目标与关闭名单）")
        return
    source = {"user_id": event.user_id,
              "group_id": event.group_id if event.is_group else None}
    count = _start_fanout(targets, _render_template(content, datetime.now()),
                          None, "定时广播目标", source)
    await _reply(event, f"测试广播已触发：正在向 {count} 个目标发送，完成后回报结果...")


# ======================== 帮助 ========================


async def handle_help(event, match):
    """/广播帮助：列出广播通知全部命令"""
    lines = [
        "【广播通知命令】",
        "— 通知发送 —",
        "  /群广播 <内容>  向全部群发通知（超管）",
        "  /群通知 <群号> <内容>  向指定群发通知（管理+）",
        "  /好友通知 <QQ> <内容>  向指定好友发通知（管理+）",
        "  /我的群  列出机器人所在群（超管）",
        "— 引用转发广播（超管）—",
        "  (引用消息)广播 [群聊|私聊|全部]",
        "  开启广播 / 关闭广播 [群聊|私聊] [序号]",
        "  广播列表 [群聊|私聊]",
        "  取消广播",
        "— 定时广播（超管）—",
        "  定时广播  查看状态",
        "  定时广播 开|关",
        "  定时广播 内容 <文本>",
        "  定时广播 cron <五段表达式>",
        "  定时广播 测试 / 重置",
        "提示：引用消息支持文本/图片/文件；通知文字支持 CQ 码",
    ]
    await _reply(event, "\n".join(lines))


# ======================== 注册入口 ========================


def register(ctx_obj):
    """插件入口：注册命令与定时任务"""
    global ctx
    ctx = ctx_obj

    # ── 通知发送 ──
    ctx.command(r"^/群广播(?:\s+([\s\S]+))?\s*$", handle_group_broadcast,
                priority=50, alias="群广播",
                description="向全部群发送通知（可引用消息转发图片/文件）",
                require_superuser=True)
    ctx.command(r"^/群通知\s+([\s\S]+)$", handle_group_notify,
                priority=50, alias="群通知",
                description="向指定群发送通知：/群通知 <群号> <内容>",
                require_admin=True)
    ctx.command(r"^/好友通知\s+([\s\S]+)$", handle_friend_notify,
                priority=50, alias="好友通知",
                description="向指定好友发送通知：/好友通知 <QQ> <内容>",
                require_admin=True)
    ctx.command(r"^/我的群\s*$", handle_my_groups,
                priority=50, alias="我的群",
                description="列出机器人所在的全部群",
                require_superuser=True)

    # ── 引用转发广播 ──
    ctx.command(r"^开启广播(?:\s+(\S+))?(?:\s+(\S+))?\s*$", handle_broadcast_enable,
                priority=50,
                description="开启广播 [群聊|私聊] [序号]",
                require_superuser=True)
    ctx.command(r"^关闭广播(?:\s+(\S+))?(?:\s+(\S+))?\s*$", handle_broadcast_disable,
                priority=50,
                description="关闭广播 [群聊|私聊] [序号]",
                require_superuser=True)
    ctx.command(r"^广播列表(?:\s+(\S+))?\s*$", handle_broadcast_list,
                priority=50,
                description="广播列表 [群聊|私聊]",
                require_superuser=True)
    ctx.command(r"^广播(?:\s+(\S+))?\s*$", handle_broadcast,
                priority=50,
                description="(引用消息)广播 [群聊|私聊|全部]",
                require_superuser=True)
    ctx.command(r"^取消广播\s*$", handle_broadcast_cancel,
                priority=50,
                description="取消当前正在进行的广播",
                require_superuser=True)

    # ── 定时广播 ──
    ctx.command(r"^定时广播(?:\s+([\s\S]+))?\s*$", handle_schedule,
                priority=50,
                description="定时广播状态与设置：开|关|内容|cron|测试|重置",
                require_superuser=True)

    # ── 帮助 ──
    ctx.command(r"^/广播帮助\s*$", handle_help,
                priority=50, alias="广播帮助",
                description="广播通知命令帮助")

    # ── 定时任务：每分钟检查定时广播 cron 是否到达触发点 ──
    ctx.task("*/1 * * * *", scheduled_broadcast_tick,
             description="定时广播调度检查")

    ctx.log("广播通知插件已加载")


def on_unload():
    """插件卸载钩子：取消群发 asyncio 任务引用，确保后台任务随插件停止"""
    task = _runtime.get("fanout_task")
    _runtime["fanout_cancel"] = True
    if task is not None:
        try:
            if not task.done():
                task.cancel()
        except Exception:
            pass
    _runtime["fanout_task"] = None
    if ctx is not None:
        ctx.log("广播通知插件已卸载")

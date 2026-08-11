"""
qqadmin 群管插件 - 自动守护模块

包含：违禁词检测(本地+API)、刷屏检测、宵禁、进群/退群/加群申请事件处理、投票禁言。
通过模块级 ctx（框架注入）访问框架能力。
"""
import time
from collections import deque

import store

# 模块级 ctx（框架注入）
ctx = None

# 刷屏记录：(gid, uid) -> deque[(timestamp, text)]
_msg_log = {}
# 消息 ID 记录：(gid, uid) -> deque[message_id]（用于撤回）
_msg_ids = {}
# 本插件开启的全员禁言群（宵禁用）
_curfew_active = set()
# 待处理加群申请：gid -> {uid: {flag, sub_type, ts}}
_pending_requests = {}
# 拒绝次数累计（进群审核）：gid -> {uid: n}
_reject_counts = {}
# 投票禁言：gid -> {target, ban_time, agree, disagree, voters, deadline, initiator}
_votes = {}


def _send(event, message):
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=message,
    )


def _send_group(gid, message):
    ctx.api("send_group_msg", group_id=gid, message=message)


# ===================== 权限判断 =====================


def can_manage(event, cfg) -> bool:
    """判断用户是否具备管理权限（超管/群主/管理员/协管）"""
    if event.role in ("super", "owner", "admin"):
        return True
    return store.is_assistant(cfg, event.user_id)


def can_manage_assistant(event) -> bool:
    """判断用户是否有权管理协管（仅群主/超管）"""
    return event.role in ("super", "owner")


# ===================== 违禁词 / 刷屏（消息兜底） =====================


def on_message_fallback(event, match):
    """兜底命令 handler：所有群消息都会经过这里（priority 最低）

    返回 False 表示未处理，让事件继续传播给其他插件。
    """
    if not event.is_group or not event.group_id:
        return False
    cfg = store.GroupConfigStore(ctx).get_group(event.group_id)

    # 记录消息 ID（供撤回命令使用）
    gid = event.group_id
    uid = event.user_id
    mid = event.message_id
    if mid is not None:
        key = (gid, uid)
        dq = _msg_ids.setdefault(key, deque(maxlen=50))
        dq.append(int(mid))

    if cfg.get("ban_word_enabled"):
        _check_ban_word(event, cfg)
    if cfg.get("spam_enabled"):
        _check_spam(event, cfg)
    return False  # 始终放行事件，让其他插件继续处理


def _check_ban_word(event, cfg) -> bool:
    """违禁词检测：支持本地词库 / 外部API / 双检模式，命中则撤回+禁言+提示"""
    text = event.message or ""
    mode = cfg.get("ban_word_mode", "local")
    hit = []
    masked = text

    if mode in ("local", "both"):
        words = store.get_ban_words(cfg)
        hit = store.find_ban_words(text, words)
        if hit:
            masked = store.mask_text(text, hit)

    # 外部 API 模式：本地未命中时调用 API
    if mode in ("api", "both"):
        if not hit:
            api_result = _check_ban_word_api(text, cfg)
            if api_result:
                hit = ["(外部API)"]
                masked = api_result

    if not hit:
        return False

    gid = event.group_id
    uid = event.user_id
    ban_time = int(cfg.get("ban_word_ban_time", 60))
    nickname = event.sender_nickname or event.sender_card or str(uid)

    try:
        if event.message_id is not None:
            ctx.api("delete_msg", message_id=int(event.message_id))
    except Exception as e:
        ctx.logger.warning(f"[qqadmin] 撤回违禁消息失败: {e}")
    try:
        ctx.ban(gid, uid, ban_time)
    except Exception as e:
        ctx.logger.warning(f"[qqadmin] 违禁禁言失败: {e}")
    _send_group(gid, f"{nickname} 的消息包含违禁词，已打码处理并禁言 {ban_time} 秒\n打码后：{masked}")
    return True


def _check_ban_word_api(text, cfg) -> str:
    """调用外部违禁词 API，命中返回打码文本，未命中/失败返回空字符串"""
    url = cfg.get("ban_word_api_url") or "https://api-v2.yuafeng.cn/API/wjc.php"
    try:
        import requests
        resp = requests.get(url, params={"text": text}, timeout=3)
        if resp.status_code != 200:
            return ""
        data = resp.json()
        if data.get("code") == 1:
            return data.get("filtered_text") or "*" * len(text)
    except Exception as e:
        ctx.logger.warning(f"[qqadmin] 违禁词API调用失败: {e}")
    return ""


def _check_spam(event, cfg) -> bool:
    """刷屏检测：窗口内同内容条数超过阈值则禁言"""
    key = (event.group_id, event.user_id)
    now = time.time()
    window = max(1, int(cfg.get("spam_window", 5)))
    threshold = max(2, int(cfg.get("spam_threshold", 5)))
    ban_time = int(cfg.get("spam_ban_time", 300))
    text = (event.message or "").strip()

    log = _msg_log.setdefault(key, deque(maxlen=50))
    log.append((now, text))
    while log and now - log[0][0] > window:
        log.popleft()

    same = sum(1 for _, t in log if t == text)
    if same >= threshold:
        _msg_log[key] = deque(maxlen=50)  # 重置计数
        try:
            ctx.ban(event.group_id, event.user_id, ban_time)
        except Exception as e:
            ctx.logger.warning(f"[qqadmin] 刷屏禁言失败: {e}")
        _send_group(
            event.group_id,
            f"{event.sender_nickname or event.user_id} 刷屏了，已被禁言 {ban_time} 秒",
        )
        return True
    return False


# ===================== 宵禁 =====================


def curfew_tick():
    """每分钟执行：检查各群是否处于宵禁窗口"""
    try:
        configs = store.GroupConfigStore(ctx)
        for gid in configs.all_group_ids():
            cfg = configs.get_group(gid)
            if not cfg.get("curfew_enabled"):
                continue
            rng = store.parse_time_range(
                f"{cfg.get('curfew_start', '23:00')} {cfg.get('curfew_end', '06:00')}"
            )
            if not rng:
                continue
            now_min = time.localtime().tm_hour * 60 + time.localtime().tm_min
            in_window = store.in_time_window(now_min, rng[0], rng[1])
            if in_window and gid not in _curfew_active:
                try:
                    ctx.api("set_group_whole_ban", group_id=gid, enable=True)
                    _curfew_active.add(gid)
                except Exception as e:
                    ctx.logger.warning(f"[qqadmin] 开启宵禁失败 {gid}: {e}")
            elif not in_window and gid in _curfew_active:
                try:
                    ctx.api("set_group_whole_ban", group_id=gid, enable=False)
                except Exception as e:
                    ctx.logger.warning(f"[qqadmin] 解除宵禁失败 {gid}: {e}")
                _curfew_active.discard(gid)
    except Exception as e:
        ctx.logger.error(f"[qqadmin] 宵禁任务异常: {e}")


def curfew_status(gid) -> bool:
    """该群当前是否处于本插件宵禁开启状态"""
    return gid in _curfew_active


# ===================== 进群 / 退群 / 加群申请 =====================


def on_group_increase(payload):
    """群成员增加：进群欢迎 / 进群禁言"""
    gid = payload.get("group_id")
    uid = payload.get("user_id")
    if not gid or not uid:
        return
    cfg = store.GroupConfigStore(ctx).get_group(gid)
    nickname = ""
    try:
        info = ctx.get_member_info(gid, uid)
        nickname = (info or {}).get("card") or (info or {}).get("nickname") or ""
    except Exception:
        pass
    if cfg.get("join_welcome"):
        msg = cfg.get("join_welcome_msg") or "欢迎新成员 {name} 加入本群！注意看群公告~"
        _send_group(gid, msg.replace("{name}", nickname or str(uid)))
    join_ban = int(cfg.get("join_ban_time", 0) or 0)
    if join_ban > 0:
        try:
            ctx.ban(gid, uid, join_ban)
        except Exception as e:
            ctx.logger.warning(f"[qqadmin] 进群禁言失败: {e}")


def on_group_decrease(payload):
    """群成员减少：退群通知 / 退群拉黑"""
    gid = payload.get("group_id")
    uid = payload.get("user_id")
    if not gid or not uid:
        return
    cfg = store.GroupConfigStore(ctx).get_group(gid)
    if cfg.get("leave_notify"):
        _send_group(gid, f"成员 {uid} 已离开本群")
    if cfg.get("leave_blacklist"):
        bl = (cfg.get("join_blacklist") or "").split()
        if str(uid) not in bl:
            bl.append(str(uid))
            store.GroupConfigStore(ctx).set_group(gid, "join_blacklist", " ".join(bl))


def on_group_request(payload):
    """加群申请：黑名单拦截 + 审核"""
    if payload.get("request_type") != "group":
        return
    sub_type = payload.get("sub_type", "add")
    if sub_type != "add":
        return
    gid = payload.get("group_id")
    uid = payload.get("user_id")
    flag = payload.get("flag", "")
    comment = payload.get("comment", "")
    if not gid or not uid or not flag:
        return

    cfg = store.GroupConfigStore(ctx).get_group(gid)
    uid_s = str(uid)

    bl = (cfg.get("join_blacklist") or "").split()
    if uid_s in bl:
        _approve(flag, sub_type, False, "您已被本群加入黑名单")
        return

    if not cfg.get("join_review"):
        return

    accept = [w for w in (cfg.get("join_accept_words") or "").split() if w]
    reject = [w for w in (cfg.get("join_reject_words") or "").split() if w]
    hit_reject = any(w in comment for w in reject)
    hit_accept = any(w in comment for w in accept)

    if hit_reject:
        _approve(flag, sub_type, False, "申请中包含不允许的关键词")
        return
    if hit_accept:
        _approve(flag, sub_type, True, "关键词匹配，自动批准")
        return

    if cfg.get("join_no_match_reject"):
        _approve(flag, sub_type, False, "未命中进群审核关键词")
        return

    max_times = int(cfg.get("join_max_times", 0) or 0)
    if max_times > 0:
        n = _reject_counts.setdefault(gid, {}).get(uid_s, 0) + 1
        _reject_counts.setdefault(gid, {})[uid_s] = n
        if n >= max_times:
            bl.append(uid_s)
            store.GroupConfigStore(ctx).set_group(gid, "join_blacklist", " ".join(bl))
            _approve(flag, sub_type, False, "进群尝试次数过多，已被拉黑")
            return

    _pending_requests.setdefault(gid, {})[uid_s] = {
        "flag": flag, "sub_type": sub_type, "ts": time.time(),
        "comment": comment[:50],
    }


def _approve(flag, sub_type, approve: bool, reason: str = ""):
    """处理加群申请"""
    try:
        ctx.api(
            "set_group_add_request",
            flag=flag, sub_type=sub_type,
            approve=approve, reason=reason,
        )
    except Exception as e:
        ctx.logger.warning(f"[qqadmin] 处理加群申请失败: {e}")


def handle_review(event, approve: bool, extra: str):
    """群内 批准/驳回 命令：extra 为 QQ 号或留空取最近申请"""
    gid = event.group_id
    if not gid:
        return
    pending = _pending_requests.get(gid, {})
    if not pending:
        _send(event, "当前没有待处理的进群申请")
        return
    uid = extra.strip() if extra else ""
    if uid and uid.isdigit():
        item = pending.get(uid)
    else:
        uid, item = max(pending.items(), key=lambda kv: kv[1]["ts"])
    if not item:
        _send(event, f"未找到 {uid} 的进群申请")
        return
    action = "批准" if approve else "驳回"
    _approve(item["flag"], item["sub_type"], approve, reason=action)
    pending.pop(uid, None)
    _send(event, f"已{action}用户 {uid} 的进群申请")


# ===================== 投票禁言 =====================


def start_vote(event, ban_time, targets):
    """发起投票禁言"""
    gid = event.group_id
    _votes[gid] = {
        "targets": targets,
        "ban_time": ban_time,
        "agree": 1,
        "disagree": 0,
        "voters": {event.user_id},
        "deadline": time.time() + 60,
        "initiator": event.user_id,
    }
    ats = " ".join(f"[CQ:at,qq={t}]" for t in targets)
    _send_group(
        gid,
        f"{event.sender_nickname or event.user_id} 发起禁言投票：{ats} 禁言 {ban_time} 秒\n"
        f"请发送「赞同禁言」或「反对禁言」投票，60 秒后统计结果",
    )


def vote(event, agree: bool):
    """赞同/反对投票"""
    gid = event.group_id
    v = _votes.get(gid)
    if not v:
        _send(event, "当前没有进行中的禁言投票")
        return
    if event.user_id in v["voters"]:
        _send(event, "你已经投过票了")
        return
    v["voters"].add(event.user_id)
    if agree:
        v["agree"] += 1
    else:
        v["disagree"] += 1
    _send(event, f"投票成功（当前 赞同 {v['agree']} / 反对 {v['disagree']}）")


def vote_tick():
    """每分钟检查：投票到期后统计并执行禁言"""
    now = time.time()
    for gid, v in list(_votes.items()):
        if now < v["deadline"]:
            continue
        _votes.pop(gid, None)
        if v["agree"] > v["disagree"]:
            for t in v["targets"]:
                try:
                    ctx.ban(gid, t, v["ban_time"])
                except Exception as e:
                    ctx.logger.warning(f"[qqadmin] 投票禁言失败: {e}")
            _send_group(gid, f"投票通过，目标已禁言 {v['ban_time']} 秒")
        else:
            _send_group(gid, "投票未通过，不执行禁言")


# ===================== 撤回辅助 =====================


def get_recent_msg_ids(gid, uid, count=10):
    """获取某用户最近的 N 条消息 ID"""
    key = (gid, uid)
    dq = _msg_ids.get(key, deque())
    return list(dq)[-count:] if dq else []
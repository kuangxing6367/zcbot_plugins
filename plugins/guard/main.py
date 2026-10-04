# -*- coding: utf-8 -*-
"""防护中心（guard）

统一防护套件：把「全局用户黑名单 / 群白名单与唤醒词 / 敏感词过滤 / 窗口限流 /
刷屏检测 / LLM 对话拦截」收进一条拦截链，集中注册、集中配置、集中观测。

拦截链（on_raw_message，priority=3，晚于会话等待器 priority=1）：
    黑名单丢弃 → 群白名单 → 唤醒词 → 敏感词 → 限流 → 刷屏检测
各环节独立开关，见 _conf_schema.json；命中策略：前五者为静默丢弃，
刷屏检测为旁路处置（自动禁言 + 群内提示，消息继续走正常流程）。

命令一览：
    /拉黑 <QQ> [理由]                    加入全局黑名单          （超管）
    /解黑 <QQ>                           移出全局黑名单          （超管）
    /黑名单                              列出全局黑名单          （超管）
    /黑名单检查 <QQ>                     查询是否在黑名单        （超管）
    /插件拉黑 <QQ> [原因]                禁止使用 LLM 对话       （超管）
    /插件取消拉黑 <QQ>                   解除 LLM 对话拦截       （超管）
    /插件黑名单                          查看 LLM 对话拦截名单   （超管）
    /spam（/防炸群 /fzq）                查看本群刷屏配置        （群内）
    /spam 开关 | 单人 a/b/c | 群体 a/b/c | 解禁                  （群管）
    /防护（/防护中心）                   查看拦截链总览          （通用）

定时任务：每 10 分钟清理限流窗口与刷屏状态（cron，不使用裸线程）。
"""
import re
import time

import guard_chain
import guard_store

__plugin_meta__ = {
    "name": "防护中心",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "统一防护套件：黑名单/白名单唤醒/敏感词/限流/刷屏检测/LLM 对话拦截，一条拦截链集中管理",
    "priority": 3,
}

ctx = None
_llm_gate = None  # 已挂到 llm_core 的对话闸门引用（卸载时移除用）


# ===================== 通用工具 =====================

async def _reply(event, message):
    """回复发起者（群内回群，私聊回私聊）"""
    try:
        await ctx.asend_msg(
            user_id=event.user_id,
            group_id=event.group_id if getattr(event, "is_group", False) else None,
            message=message,
        )
    except Exception as e:
        try:
            ctx.log(f"回复发送失败: {e}", level="warning")
        except Exception:
            pass


def _is_super(event) -> bool:
    """超管判断（事件属性 + users 表双重兜底）"""
    if getattr(event, "is_superuser", False):
        return True
    if str(getattr(event, "role", "") or "") == "super":
        return True
    try:
        rows = ctx.db_query(
            "SELECT 1 FROM users WHERE user_id=%s AND role='super'",
            (str(event.user_id),),
        ) or []
        return bool(rows)
    except Exception:
        return False


def _need_admin(event) -> bool:
    """群管判断：超管 / 群主 / 群管理员"""
    return getattr(event, "is_admin", False) or \
        getattr(event, "role", "") in ("super", "owner", "admin")


def _fmt_time(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts or 0)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "-"


# ===================== 全局用户黑名单命令 =====================

async def cmd_block_add(event, match):
    """/拉黑 <QQ号> [理由] —— 加入全局黑名单，此后其消息被静默丢弃"""
    if not _is_super(event):
        await _reply(event, "需要超级管理员权限")
        return
    text = (match.group(1) if match else "").strip()
    m = re.match(r"(\d{5,})\s*(.*)", text)
    if not m:
        await _reply(event, "用法: /拉黑 <QQ号> [理由]")
        return
    uid = int(m.group(1))
    reason = m.group(2).strip()
    try:
        await guard_store.add_block_user(uid, reason)
    except Exception as e:
        await _reply(event, f"拉黑失败: {e}")
        return
    guard_chain.block_ids_add(uid)
    await _reply(event, f"已将 {uid} 加入全局黑名单"
                 + (f"（理由：{reason}）" if reason else ""))


async def cmd_block_remove(event, match):
    """/解黑 <QQ号> —— 移出全局黑名单"""
    if not _is_super(event):
        await _reply(event, "需要超级管理员权限")
        return
    text = (match.group(1) if match else "").strip()
    m = re.search(r"(\d{5,})", text)
    if not m:
        await _reply(event, "用法: /解黑 <QQ号>")
        return
    uid = int(m.group(1))
    try:
        await guard_store.remove_block_user(uid)
    except Exception as e:
        await _reply(event, f"解黑失败: {e}")
        return
    guard_chain.block_ids_discard(uid)
    await _reply(event, f"已将 {uid} 移出全局黑名单")


async def cmd_block_list(event, match):
    """/黑名单 —— 列出全局黑名单（最多展示 50 条）"""
    if not _is_super(event):
        await _reply(event, "需要超级管理员权限")
        return
    try:
        rows = await guard_store.list_block_users()
    except Exception as e:
        await _reply(event, f"查询失败: {e}")
        return
    if not rows:
        await _reply(event, "黑名单为空")
        return
    lines = [f"🚫 全局黑名单（{len(rows)} 人）："]
    for r in rows[:50]:
        reason = r.get("reason") or ""
        lines.append(f"  {r['user_id']}" + (f" — {reason}" if reason else ""))
    if len(rows) > 50:
        lines.append(f"…… 还有 {len(rows) - 50} 条未显示")
    await _reply(event, "\n".join(lines))


async def cmd_block_check(event, match):
    """/黑名单检查 <QQ号> —— 查询某 QQ 是否在全局黑名单"""
    text = (match.group(1) if match else "").strip()
    m = re.search(r"(\d{5,})", text)
    if not m:
        await _reply(event, "用法: /黑名单检查 <QQ号>")
        return
    uid = int(m.group(1))
    await guard_chain.ensure_block_ids(force=True)
    hit = guard_chain.block_contains(uid)
    await _reply(event, f"{uid} {'在' if hit else '不在'}全局黑名单中")


# ===================== LLM 对话拦截命令 =====================

async def cmd_llm_add(event, match):
    """/插件拉黑 <QQ号> [原因] —— 禁止该用户使用 LLM 对话"""
    if not _is_super(event):
        await _reply(event, "❌ 权限不足：仅超级管理员可执行此操作。")
        return
    uid = (match.group(1) or "").strip()
    reason = (match.group(2) or "").strip()
    if not uid or not uid.isdigit():
        await _reply(event, "❌ 用法错误：/插件拉黑 <QQ号> [原因]，QQ号必须是数字。")
        return
    try:
        await guard_store.add_llm_block(uid, reason, str(event.user_id))
    except Exception as e:
        ctx.log(f"LLM 拉黑失败: {e}", level="error")
        await _reply(event, f"❌ 拉黑失败：{e}")
        return
    await _reply(
        event,
        f"🚫 已将 {uid} 加入 LLM 对话黑名单，无法再使用对话功能。"
        + (f"\n原因：{reason}" if reason else "")
        + "\n解除请发：/插件取消拉黑 " + uid,
    )


async def cmd_llm_remove(event, match):
    """/插件取消拉黑 <QQ号> —— 解除 LLM 对话拦截"""
    if not _is_super(event):
        await _reply(event, "❌ 权限不足：仅超级管理员可执行此操作。")
        return
    uid = (match.group(1) or "").strip()
    if not uid or not uid.isdigit():
        await _reply(event, "❌ 用法错误：/插件取消拉黑 <QQ号>，QQ号必须是数字。")
        return
    try:
        await guard_store.remove_llm_block(uid)
        still = await guard_store.llm_block_exists(uid)
    except Exception as e:
        ctx.log(f"LLM 解除拉黑失败: {e}", level="error")
        await _reply(event, f"❌ 解除拉黑失败：{e}")
        return
    if still:
        await _reply(event, f"❌ 解除失败：{uid} 仍在名单中，请重试。")
        return
    await _reply(event, f"✅ 已解除 {uid} 的拉黑，可以正常使用 LLM 对话了。")


async def cmd_llm_list(event, match):
    """/插件黑名单 —— 查看 LLM 对话拦截名单"""
    if not _is_super(event):
        await _reply(event, "❌ 权限不足：仅超级管理员可执行此操作。")
        return
    try:
        rows = await guard_store.list_llm_block()
    except Exception as e:
        ctx.log(f"LLM 名单查询失败: {e}", level="error")
        await _reply(event, f"❌ 查询失败：{e}")
        return
    if not rows:
        await _reply(event, "📭 当前 LLM 对话黑名单为空。")
        return
    try:
        limit = int(ctx.get_config("llm_list_limit", 20) or 20)
    except (TypeError, ValueError):
        limit = 20
    lines = [f"🚫 LLM 对话黑名单（共 {len(rows)} 人）："]
    for i, r in enumerate(rows[:limit], 1):
        uid = r.get("user_id") or ""
        reason = r.get("reason") or ""
        ts = r.get("created_at") or 0
        line = f"{i}. {uid}"
        if reason:
            line += f"（{reason}）"
        line += f"  [{_fmt_time(ts)}]"
        lines.append(line)
    if len(rows) > limit:
        lines.append(f"…… 还有 {len(rows) - limit} 条未显示")
    await _reply(event, "\n".join(lines))


# ===================== 刷屏检测命令（/spam 族）=====================

def _spam_settings_text(s: dict) -> str:
    """渲染本群刷屏配置文本"""
    return "\n".join([
        "━━━━ 刷屏检测设置 ━━━━",
        f"总开关：{'开启' if s['enabled'] else '关闭'}",
        f"单人：{s['user_window']} 秒内 {s['user_threshold']} 条 → 禁言 {s['user_mute']} 秒",
        f"群体：{s['group_window']} 秒内 {s['group_threshold']} 条 → 全体禁言 {s['group_mute']} 秒",
        f"管理员豁免：{'是' if s['exempt_admin'] else '否'}",
        f"处置提示：{'开' if s['notify'] else '关'}",
        "━━━━━━━━━━━━━━━",
        "改配置：/spam 单人 10/5/300    /spam 群体 25/5/600    /spam 开关",
    ])


async def cmd_spam_status(event, match):
    """/spam —— 查看本群刷屏检测配置"""
    if not getattr(event, "is_group", False):
        await _reply(event, "该命令请在群内使用")
        return
    s = await guard_chain.get_spam_settings(event.group_id)
    await _reply(event, _spam_settings_text(s))


async def cmd_spam_toggle(event, match):
    """/spam 开关 —— 开启/关闭本群刷屏检测"""
    if not getattr(event, "is_group", False):
        return
    if not _need_admin(event):
        await _reply(event, "需要管理员权限")
        return
    s = await guard_chain.get_spam_settings(event.group_id)
    new_val = 0 if s['enabled'] else 1
    if await guard_chain.save_spam_settings(event.group_id, {'enabled': new_val}):
        await _reply(event, f"刷屏检测已{'开启' if new_val else '关闭'}")


async def cmd_spam_set(event, match):
    """/spam 单人 10/5/300 或 /spam 群体 25/5/600 —— 设置阈值与禁言时长"""
    if not getattr(event, "is_group", False):
        return
    if not _need_admin(event):
        await _reply(event, "需要管理员权限")
        return
    body = (match.group(1) or "").strip()
    m = re.match(r"^(单人|群体)\s*([\s\S]+)$", body)
    if not m:
        await _reply(event, "用法：/spam 单人 10/5/300    /spam 群体 25/5/600")
        return
    prefix = 'user' if m.group(1) == '单人' else 'group'
    label = '单人刷屏' if prefix == 'user' else '群体刷屏'
    # 数字解析：支持「10/5/300」「10 5 300」「10，5，300」等写法，取前三个正整数
    parts = [p for p in m.group(2).replace('/', ' ')
             .replace('，', ' ').replace(',', ' ').split() if p]
    nums = []
    for p in parts[:3]:
        try:
            nums.append(int(p))
        except ValueError:
            break
    if len(nums) != 3 or any(n <= 0 for n in nums):
        await _reply(event, "需要三个正整数：条数/窗口秒/禁言秒")
        return
    patch = {f'{prefix}_threshold': nums[0],
             f'{prefix}_window': nums[1],
             f'{prefix}_mute': nums[2]}
    if await guard_chain.save_spam_settings(event.group_id, patch):
        await _reply(event, f"{label}已设为 {nums[1]} 秒内 {nums[0]} 条 → 禁言 {nums[2]} 秒")


async def cmd_spam_unmute(event, match):
    """/spam 解禁 —— 立即解除本群全体禁言"""
    if not getattr(event, "is_group", False):
        return
    if not _need_admin(event):
        await _reply(event, "需要管理员权限")
        return
    guard_chain.clear_group_mute(event.group_id)
    try:
        await ctx.amute_all(event.group_id, False)
        await _reply(event, "已解除全体禁言")
    except Exception as e:
        await _reply(event, f"解除失败：{e}")


# ===================== 防护总览命令 =====================

async def cmd_overview(event, match):
    """/防护 —— 查看拦截链各环节开关与运行概况"""
    cfg = guard_chain.get_config()
    watching, muted = guard_chain.spam_snapshot()
    blocked = await guard_store.count_block_users()
    llm_blocked = await guard_store.count_llm_block()
    wake_on = bool(cfg.get('wake_enable') or cfg.get('wake_private'))
    prefixes = cfg.get('wake_prefixes', [])
    lines = [
        "━━━━ 防护中心 ━━━━",
        "拦截链：黑名单 → 群白名单/唤醒词 → 敏感词 → 限流 → 刷屏检测",
        f"1. 全局黑名单：{blocked} 人（命中静默丢弃）",
        f"2. 群白名单：{'开' if cfg.get('whitelist_enable') else '关'}，"
        f"{len(cfg.get('whitelist_groups', []))} 群",
        f"3. 唤醒词：{'开' if wake_on else '关'}，"
        f"前缀：{'、'.join(prefixes) if prefixes else '无'}",
        f"4. 敏感词：{'开' if cfg.get('sensitive_enable') else '关'}，"
        f"{len(cfg.get('sensitive_words', []))} 词",
        f"5. 限流：{'开' if cfg.get('rate_enable') else '关'}，"
        f"{cfg.get('rate_seconds', 10)} 秒内 {cfg.get('rate_count', 5)} 条",
        f"6. 刷屏检测：{watching} 群监控中，{muted} 群全体禁言中（群内 /spam 查看配置）",
        f"7. LLM 对话拦截：{llm_blocked} 人",
        "━━━━━━━━━━━━━━━",
    ]
    await _reply(event, "\n".join(lines))


# ===================== LLM 对话闸门 =====================

def _make_llm_gate():
    """生成 llm_core 对话闸门：命中拦截名单返回拒绝提示，否则放行（None）。

    数据库故障时放行（不拦截对话），避免存储异常影响正常用户。
    """
    def gate(user_id):
        if not guard_store.llm_gate_hit(user_id):
            return None
        try:
            tip = ctx.get_config("llm_block_tip", "") or "你已被拉黑，无法使用 LLM 对话。"
        except Exception:
            tip = "你已被拉黑，无法使用 LLM 对话。"
        return str(tip)
    return gate


def _wire_llm_gate(*_args):
    """把闸门挂到 llm_core 服务（对方未加载时挂不上，返回 False 等事件重试）"""
    global _llm_gate
    try:
        svc = ctx._framework.services.get('llm_core')
        if svc is not None and hasattr(svc, 'register_chat_gate'):
            if _llm_gate is None:
                _llm_gate = _make_llm_gate()
            svc.register_chat_gate(_llm_gate)
            ctx.log("LLM 对话闸门已挂到 llm_core")
            return True
    except Exception as e:
        ctx.log(f"挂载 LLM 对话闸门失败: {e}", level="warning")
    return False


# ===================== 仪表盘卡片 =====================

def card_overview():
    """仪表盘「防护总览」卡片：拦截链启用环节 + 黑名单/监控概况"""
    try:
        cfg = guard_chain.get_config()
        watching, muted = guard_chain.spam_snapshot()
        blocked = guard_chain.block_count()
        stages = []
        if blocked:
            stages.append("黑名单")
        if cfg.get("whitelist_enable"):
            stages.append("白名单")
        if cfg.get("wake_enable") or cfg.get("wake_private"):
            stages.append("唤醒")
        if cfg.get("sensitive_enable"):
            stages.append("敏感词")
        if cfg.get("rate_enable"):
            stages.append("限流")
        stages.append("刷屏")
        return {
            "value": f"{'/'.join(stages)} · 监控 {watching} 群 · 禁言中 {muted} 群",
            "label": f"防护总览（黑名单 {blocked} 人）",
        }
    except Exception:
        return {"value": "N/A", "label": "防护总览"}


# ===================== 定时任务 =====================

def cleanup_task():
    """定时清理限流窗口与刷屏状态（调度器按名查找，需保留在 main 模块命名空间）"""
    guard_chain.cleanup()


# ===================== 注册入口 =====================

def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    guard_store.init(ctx)
    guard_chain.init(ctx)

    # 建表（幂等；失败仅告警，内存型环节不受影响）
    try:
        guard_store.create_tables()
    except Exception as e:
        ctx.log(f"建表失败，名单与群配置将退化为默认行为: {e}", level="error")

    # 统一拦截链（执行顺序由 __plugin_meta__ priority=3 表达：
    # 晚于会话等待器(1)、先于普通插件；刷屏检测为旁路处置，不吞消息）
    ctx.on_raw_message(guard_chain.on_raw)

    # 定时清理（cron 调度，不使用裸线程）
    ctx.task("*/10 * * * *", cleanup_task,
             description="清理限流窗口与刷屏状态")

    # ── 全局用户黑名单（超管）──
    ctx.command(r"^/拉黑\s+([\s\S]+)", cmd_block_add, priority=50,
                description="加入全局黑名单：/拉黑 <QQ号> [理由]",
                require_superuser=True)
    ctx.command(r"^/解黑\s+([\s\S]+)", cmd_block_remove, priority=50,
                description="移出全局黑名单：/解黑 <QQ号>",
                require_superuser=True)
    ctx.command(r"^/黑名单\s*$", cmd_block_list, priority=50,
                description="列出全局黑名单", require_superuser=True)
    ctx.command(r"^/黑名单检查\s+([\s\S]+)", cmd_block_check, priority=50,
                description="查询是否在黑名单：/黑名单检查 <QQ号>",
                require_superuser=True)

    # ── LLM 对话拦截名单（超管）──
    ctx.command(r"^/插件拉黑\s+(\d+)\s*(.*)$", cmd_llm_add, priority=55,
                description="拉黑用户(禁止使用LLM对话)：/插件拉黑 <QQ号> [原因]",
                require_superuser=True)
    ctx.command(r"^/插件取消拉黑\s+(\d+)\s*$", cmd_llm_remove, priority=55,
                description="解除 LLM 对话拉黑：/插件取消拉黑 <QQ号>",
                require_superuser=True)
    ctx.command(r"^/插件黑名单\s*$", cmd_llm_list, priority=55,
                description="查看 LLM 对话黑名单列表",
                require_superuser=True)

    # ── 刷屏检测（/spam 族；子命令优先级高于查看命令，避免「/spam 开关」被查看吞掉）──
    ctx.command(r"^/spam\s*开关$", cmd_spam_toggle, priority=19,
                description="开启/关闭本群刷屏检测")
    ctx.command(r"^/spam\s*(?:单人|群体)\s+([\s\S]+)", cmd_spam_set, priority=19,
                description="设置刷屏阈值：/spam 单人 10/5/300")
    ctx.command(r"^/spam\s*解禁$", cmd_spam_unmute, priority=19,
                description="立即解除本群全体禁言")
    ctx.command(r"^/spam(?:\s+([\s\S]*))?$", cmd_spam_status, priority=20,
                alias="/防炸群,/fzq", description="查看本群刷屏检测配置")

    # ── 防护总览 ──
    ctx.command(r"^/防护\s*$", cmd_overview, priority=20,
                alias="/防护中心", description="查看防护拦截链总览")

    # ── LLM 对话闸门（llm_core 未加载时监听加载完成事件重试）──
    if not _wire_llm_gate():
        try:
            ctx.on("system.plugin.loaded", _wire_llm_gate)
        except Exception:
            pass

    # ── 仪表盘卡片 ──
    ctx.dashboard_card("防护总览", card_overview, icon="🛡", priority=12)

    ctx.log("防护中心已加载：拦截链就绪（priority=3），/防护 查看总览")


def on_unload():
    """卸载清理：移除对话闸门、清空内存状态"""
    global _llm_gate
    if _llm_gate is not None:
        try:
            svc = ctx._framework.services.get('llm_core')
            if svc is not None and hasattr(svc, 'unregister_chat_gate'):
                svc.unregister_chat_gate(_llm_gate)
        except Exception:
            pass
        _llm_gate = None
    guard_chain.clear_state()

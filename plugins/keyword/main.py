# -*- coding: utf-8 -*-
"""
关键词引擎：统一的「触发 → 动作」规则套件
==========================================
规则模型 = 触发（关键词 / 正则） + 动作（自动回复 / 调用外部 API / 私聊通知超管）
         + 作用域（本群 / 全局） + 启用禁用 + 命中统计

- 自动回复动作：添加 / 添加正则 / 添加全局 / 添加全局正则 / 删除 / 删除全局 /
  启用 / 禁用 / 查看回复 / 查看全局回复 / 回复统计 / 回复帮助
- API 动作：/关键词api（别名 /kwapi，超管）：添加/列表/删除/启用/禁用/测试；
  规则注册为框架动态命令，命中关键词自动调用外部接口并回复
- 监控通知动作：/监控 /取消监控 /监控列表（超管），群消息命中后私聊全部超级管理员

多轮会话（添加规则后等待下一条消息作为回复内容）优先通过框架会话等待器实现
（sys.modules.get("plugin_session_waiter")），会话等待器未加载时回退插件内自建会话表。

命中处理顺序（群普通文本消息）：
  监控通知（仅观察、不接管） → 自动回复（命中即接管） → API 动作（经动态命令由路由兜底触发）
"""
import asyncio
import re
import sys
import threading
import time

import engine

__plugin_meta__ = {
    "name": "关键词引擎",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "关键词/正则统一触发引擎：自动回复、外部 API 调用、监控私聊通知，支持本群/全局作用域与命中统计",
    "priority": 40,
}

ctx = None

ADD_SESSION_TTL = 600        # 添加规则时等待回复内容的有效期（秒）

GLOBAL_SCOPE = engine.GLOBAL_SCOPE  # 全局作用域标识（群号字段为 "0"）

# 管理命令前缀：这些消息交给命令流程处理，不作为回复内容，也不参与关键词匹配
_CMD_PREFIXES = (
    "回复帮助", "查看回复", "添加全局正则", "添加全局", "添加正则", "添加 ",
    "删除全局", "删除 ", "启用 ", "禁用 ", "查看全局回复", "回复统计",
)

HELP_TEXT = """━━━━━━━━━━━━━━━
 关键词引擎
 ━━━━━━━━━━━━━━━
 【管理员指令】管理本群规则:
 - 添加 [关键词] - 添加本群自动回复
 - 添加正则 [正则] - 添加正则自动回复
 - 删除 [关键词] - 删除规则(含正则)
 - 查看回复 - 查看本群规则列表
 - 启用 [关键词] - 启用规则
 - 禁用 [关键词] - 禁用规则

 【超级管理员指令】管理所有群:
 - 添加全局 [关键词] - 添加全局自动回复
 - 添加全局正则 [正则] - 添加全局正则自动回复
 - 删除全局 [关键词] - 删除全局规则
 - 查看全局回复 - 查看所有全局规则

 - 回复统计 - 查看规则数量与命中次数

 【API 动作】(超级管理员):
 - /关键词api - 关键词触发调用外部接口

 【监控通知】(超级管理员):
 - /监控 [关键词] - 命中后私聊通知
 - /取消监控 [关键词] - 取消监控
 - /监控列表 - 查看监控词

 提示:
 - 添加命令发出后直接发送回复内容即可完成
 - 回复内容支持文字与 [CQ:image,file=...] 图片
 - 添加关键词和回复内容时可选开启违禁词检测，
   含违禁词的内容将被拒绝添加
 ━━━━━━━━━━━━━━━"""

_KWAPI_HELP = (
    "🔑 关键词API 管理（超管）\n"
    "添加规则：/关键词api 添加 <关键词> <API地址> [--匹配 exact|prefix|contains|regex] "
    "[--方法 GET|POST] [--参数 '{\"q\":\"{arg}\"}'] [--请求头 '{\"Authorization\":\"xx\"}'] "
    "[--提取 data.content] [--前缀 回复前缀]\n"
    "查看列表：/关键词api 列表\n"
    "删除规则：/关键词api 删除 <id>\n"
    "启用/禁用：/关键词api 启用 <id> ｜ /关键词api 禁用 <id>\n"
    "手动测试：/关键词api 测试 <id> [内容]\n"
    "参数模板：{arg}=关键词后内容 ｜ {msg}=整条消息 ｜ {url}/{urls}=消息中的链接\n"
    "示例：/关键词api 添加 天气 https://api.example.com/weather --参数 '{\"city\":\"{arg}\"}' --提取 data.weather"
)

# 回退用会话表：user_id -> 待输入回复内容的状态（会话等待器不可用时启用）
_PENDING = {}
_PENDING_LOCK = threading.RLock()


# ================= 基础工具 =================

def _conf(key, default=None):
    return engine.conf(key, default)


async def _reply(event, text):
    """命令流程中回复（自动判断私聊/群聊）"""
    try:
        await ctx.asend_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message=text,
        )
    except Exception as e:
        ctx.log(f"消息发送失败: {e}", level="warning")


async def _send_raw(raw_event, text):
    """原始消息流程中回复：群消息回群聊，私聊回私聊"""
    gid = raw_event.get("group_id")
    try:
        await ctx.asend_msg(
            user_id=raw_event.get("user_id"),
            group_id=gid if gid else None,
            message=text,
        )
    except Exception as e:
        ctx.log(f"回复发送失败: {e}", level="warning")


def _send(target, text):
    """同步发送（超管管理命令用）"""
    try:
        ctx.send_msg(**target, message=text)
    except Exception as e:
        ctx.log(f"发送失败: {e}", level="warning")


def _preview(text, limit):
    text = str(text or "")
    return text[:limit] + "..." if len(text) > limit else text


def _arg(event) -> str:
    """取命令后参数文本（命令名与参数以空白分隔）"""
    parts = (event.message or "").strip().split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _is_super(user_id) -> bool:
    try:
        return bool(ctx.is_superuser(int(user_id)))
    except Exception:
        return False


def _is_group_admin(group_id, user_id) -> bool:
    try:
        return ctx.get_user_role(int(group_id), int(user_id)) in ("super", "owner", "admin")
    except Exception:
        return False


def _looks_like_command(text) -> bool:
    """判断消息是否为命令（命令消息不作为回复内容，也不被会话吞掉）"""
    return text.startswith("/") or text.startswith(_CMD_PREFIXES)


def _sw():
    """获取框架会话等待器模块（priority=1 最先加载，正常必在；取不到返回 None）"""
    return sys.modules.get("plugin_session_waiter")


# ================= 添加规则的会话流程 =================

def _session_filter(raw_event) -> bool:
    """会话等待器过滤器：返回 True 消费该消息并结束等待；命令消息返回 False 放行命令流程"""
    text = engine.extract_text(raw_event.get("message", "")).strip()
    return bool(text) and not _looks_like_command(text)


async def _start_add(event, keyword, is_global, is_regex):
    """添加规则第一步：校验关键词 → 违禁词检测 → 等待下一条消息作为回复内容"""
    if not event.is_group:
        await _reply(event, "❌ 请在群内使用")
        return
    keyword = (keyword or "").strip()
    if not keyword:
        usage = "正则表达式" if is_regex else "关键词"
        _scope = "添加全局" if is_global else "添加"
        await _reply(event, f"❌ 格式错误\n格式: {_scope}{'正则' if is_regex else ''} {usage}")
        return
    if is_regex:
        try:
            re.compile(keyword)
        except re.error as e:
            await _reply(event, f"❌ 正则表达式错误: {e}")
            return
    forbidden = await asyncio.to_thread(engine.check_forbidden_sync, keyword)
    if forbidden:
        await _reply(event, f"⚠️ 关键词包含违禁词，添加失败\n检测到: {forbidden}")
        return

    scope = GLOBAL_SCOPE if is_global else str(event.group_id)
    scope_name = "全局" if is_global else "本群"
    reg_tag = "正则" if is_regex else ""
    prompt = (f"📝 请输入{scope_name}{reg_tag}回复内容\n"
              f"关键词: {keyword}\n"
              f"直接发送回复内容即可（支持文字与 CQ 码图片，10 分钟内有效）")

    sw = _sw()
    if sw is not None:
        # 优先走框架会话等待器：等待同一用户同一群的下一条普通消息
        await _reply(event, prompt)
        raw = await sw.wait_for_user(ctx, sw.make_session_id(event),
                                     timeout=ADD_SESSION_TTL, handler=_session_filter)
        if raw is None:
            await _reply(event, "⌛ 输入超时，本次添加已取消，请重新发送添加命令")
            return
        text = engine.extract_text(raw.get("message", "")).strip()
        await _finish_add(event, scope, keyword, is_regex, is_global, text)
    else:
        # 回退方案：登记到插件内会话表，由原始消息处理流程接管下一条普通消息
        with _PENDING_LOCK:
            _PENDING[str(event.user_id)] = {
                "keyword": keyword,
                "is_regex": bool(is_regex),
                "is_global": bool(is_global),
                "scope": scope,
                "ts": time.time(),
            }
        await _reply(event, prompt)


async def _finish_add(event, scope, keyword, is_regex, is_global, text):
    """添加规则第二步：校验回复内容 → 违禁词检测 → 写入规则"""
    if is_global and not _is_super(event.user_id):
        await _reply(event, "❌ 权限不足，仅超级管理员可添加全局回复")
        return
    if not text:
        await _reply(event, "❌ 回复内容为空，添加已取消")
        return
    forbidden = await asyncio.to_thread(engine.check_forbidden_sync, text)
    if forbidden:
        await _reply(event, f"⚠️ 回复内容包含违禁词，添加失败\n检测到: {forbidden}")
        return
    await asyncio.to_thread(engine.upsert_reply_rule, scope, keyword, text, is_regex, event.user_id)
    scope_name = "全局" if is_global else "本群"
    reg_tag = "正则" if is_regex else ""
    await _reply(event,
                 f"✅ {scope_name}{reg_tag}回复添加成功\n"
                 f"关键词: {keyword}\n回复: {_preview(text, 30)}")


async def _handle_pending(raw_event, uid, gid, text) -> bool:
    """回退会话处理（会话等待器不可用时）；返回 True 表示该消息被接管"""
    with _PENDING_LOCK:
        session = _PENDING.get(uid)
    if not session:
        return False
    if time.time() - session.get("ts", 0) > ADD_SESSION_TTL:
        with _PENDING_LOCK:
            _PENDING.pop(uid, None)
        return False

    if session.get("is_global"):
        if not _is_super(uid):
            with _PENDING_LOCK:
                _PENDING.pop(uid, None)
            await _send_raw(raw_event, "❌ 权限不足，仅超级管理员可添加全局回复")
            return True
    else:
        if session.get("scope") != gid:
            return False  # 已切换群聊：不吞消息，会话保留原样
        if not _is_group_admin(gid, uid):
            with _PENDING_LOCK:
                _PENDING.pop(uid, None)
            await _send_raw(raw_event, "❌ 仅管理员或群主可添加回复")
            return True

    forbidden = await asyncio.to_thread(engine.check_forbidden_sync, text)
    if forbidden:
        with _PENDING_LOCK:
            _PENDING.pop(uid, None)
        await _send_raw(raw_event, f"⚠️ 回复内容包含违禁词，添加失败\n检测到: {forbidden}")
        return True

    keyword = session.get("keyword", "")
    await asyncio.to_thread(engine.upsert_reply_rule,
                            session.get("scope"), keyword, text,
                            session.get("is_regex"), uid)
    with _PENDING_LOCK:
        _PENDING.pop(uid, None)
    scope_name = "全局" if session.get("is_global") else "本群"
    reg_tag = "正则" if session.get("is_regex") else ""
    await _send_raw(raw_event,
                    f"✅ {scope_name}{reg_tag}回复添加成功\n"
                    f"关键词: {keyword}\n回复: {_preview(text, 30)}")
    return True


def _cleanup_pending():
    """定时清理过期的回退会话（防内存增长）"""
    now = time.time()
    with _PENDING_LOCK:
        for k in [k for k, s in _PENDING.items() if now - s.get("ts", 0) > ADD_SESSION_TTL]:
            _PENDING.pop(k, None)


# ================= 自动回复规则管理命令 =================

async def handle_add(event, match=None):
    await _start_add(event, _arg(event), is_global=False, is_regex=False)


async def handle_add_regex(event, match=None):
    await _start_add(event, _arg(event), is_global=False, is_regex=True)


async def handle_add_global(event, match=None):
    await _start_add(event, _arg(event), is_global=True, is_regex=False)


async def handle_add_global_regex(event, match=None):
    await _start_add(event, _arg(event), is_global=True, is_regex=True)


async def handle_delete(event, match=None):
    if not event.is_group:
        await _reply(event, "❌ 请在群内使用")
        return
    keyword = _arg(event)
    if not keyword:
        await _reply(event, "❌ 格式错误\n格式: 删除 关键词")
        return
    n = await asyncio.to_thread(engine.delete_reply_rule, str(event.group_id), keyword)
    if n:
        await _reply(event, f"✅ 回复删除成功\n关键词: {keyword}")
    else:
        await _reply(event, "❌ 未找到该回复")


async def handle_delete_global(event, match=None):
    keyword = _arg(event)
    if not keyword:
        await _reply(event, "❌ 格式错误\n格式: 删除全局 关键词")
        return
    n = await asyncio.to_thread(engine.delete_reply_rule, GLOBAL_SCOPE, keyword)
    if n:
        await _reply(event, f"✅ 全局回复删除成功\n关键词: {keyword}")
    else:
        await _reply(event, "❌ 未找到该回复")


async def handle_enable(event, match=None):
    if not event.is_group:
        await _reply(event, "❌ 请在群内使用")
        return
    keyword = _arg(event)
    if not keyword:
        await _reply(event, "❌ 格式错误\n格式: 启用 关键词")
        return
    n = await asyncio.to_thread(engine.set_reply_enabled, str(event.group_id), keyword, True)
    if n:
        await _reply(event, f"✅ 回复已启用\n关键词: {keyword}")
    else:
        await _reply(event, "❌ 未找到该回复")


async def handle_disable(event, match=None):
    if not event.is_group:
        await _reply(event, "❌ 请在群内使用")
        return
    keyword = _arg(event)
    if not keyword:
        await _reply(event, "❌ 格式错误\n格式: 禁用 关键词")
        return
    n = await asyncio.to_thread(engine.set_reply_enabled, str(event.group_id), keyword, False)
    if n:
        await _reply(event, f"✅ 回复已禁用\n关键词: {keyword}")
    else:
        await _reply(event, "❌ 未找到该回复")


def _rule_line(r):
    tag = "[正则] " if r.get("is_regex") else ""
    off = " [已禁用]" if not r.get("is_enabled") else ""
    return f"{tag}{_preview(r.get('keyword', ''), 30)}{off}（命中 {int(r.get('hit_count', 0))} 次）"


async def handle_list(event, match=None):
    if not event.is_group:
        await _reply(event, "❌ 请在群内使用")
        return
    rules = await asyncio.to_thread(engine.reply_rules_in_scope, str(event.group_id))
    if not rules:
        await _reply(event, "本群暂无自定义回复")
        return
    lines = ["━━━━━━━━━━━━━━━\n本群自定义回复列表\n━━━━━━━━━━━━━━━"]
    for i, r in enumerate(rules, 1):
        img = " [图]" if "[CQ:image" in str(r.get("reply_content", "")) else ""
        lines.append(f"{i}. {_rule_line(r)}")
        lines.append(f"   回复: {_preview(r.get('reply_content', ''), 50)}{img}")
    lines.append("━━━━━━━━━━━━━━━")
    lines.append(f"共 {len(rules)} 条回复")
    await _reply(event, "\n".join(lines))


async def handle_list_global(event, match=None):
    rules = await asyncio.to_thread(engine.reply_rules_in_scope, GLOBAL_SCOPE)
    if not rules:
        await _reply(event, "暂无全局自定义回复")
        return
    lines = ["━━━━━━━━━━━━━━━\n全局自定义回复列表\n━━━━━━━━━━━━━━━"]
    for i, r in enumerate(rules, 1):
        img = " [图]" if "[CQ:image" in str(r.get("reply_content", "")) else ""
        lines.append(f"{i}. {_rule_line(r)}")
        lines.append(f"   回复: {_preview(r.get('reply_content', ''), 50)}{img}")
    lines.append("━━━━━━━━━━━━━━━")
    lines.append(f"共 {len(rules)} 条全局回复")
    await _reply(event, "\n".join(lines))


async def handle_stats(event, match=None):
    if not event.is_group:
        await _reply(event, "❌ 请在群内使用")
        return
    group_rules = await asyncio.to_thread(engine.reply_rules_in_scope, str(event.group_id))
    global_rules = await asyncio.to_thread(engine.reply_rules_in_scope, GLOBAL_SCOPE)
    g_hits = sum(int(r.get("hit_count", 0)) for r in group_rules)
    gl_hits = sum(int(r.get("hit_count", 0)) for r in global_rules)
    await _reply(event,
                 f"本群自定义回复 {len(group_rules)} 条（累计命中 {g_hits} 次）\n"
                 f"全局自定义回复 {len(global_rules)} 条（累计命中 {gl_hits} 次）")


async def handle_help(event, match=None):
    await _reply(event, HELP_TEXT)


# ================= API 动作管理命令（超管，同步 handler） =================

def cmd_main(event, match):
    """超管管理命令：/关键词api <子命令>"""
    # 权限防御（框架已校验 super，这里再兜底）
    if not getattr(event, "is_superuser", False):
        return False
    args = (match.group(1) if match else "").strip()
    parts = args.split(maxsplit=1)
    sub = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    target = {"group_id": event.group_id} if event.is_group else {"user_id": event.user_id}

    if not sub or sub in ("帮助", "help", "h", "?"):
        _send(target, _KWAPI_HELP)
        return True
    if sub in ("列表", "list", "ls"):
        _send(target, _api_list())
        return True
    if sub in ("添加", "add", "新增"):
        _send(target, _api_add(rest, event.user_id))
        return True
    if sub in ("删除", "del", "delete", "remove"):
        _send(target, _api_delete(rest))
        return True
    if sub in ("启用", "enable", "on"):
        _send(target, _api_set_enabled(rest, 1))
        return True
    if sub in ("禁用", "disable", "off"):
        _send(target, _api_set_enabled(rest, 0))
        return True
    if sub in ("测试", "test"):
        _send(target, _api_test(rest))
        return True
    _send(target, f"未知子命令: {sub}\n\n{_KWAPI_HELP}")
    return True


def _api_list():
    rules = [r for r in engine.load_rules() if r["action_type"] == engine.ACTION_API]
    if not rules:
        return "📋 暂无规则，用 /关键词api 添加 <关键词> <API地址> 创建"
    lines = ["📋 关键词API 规则列表："]
    for r in rules:
        status = "✅" if r["is_enabled"] else "⛔"
        lines.append(
            f"{status} #{r['id']} [{r['match_type']}] {r['keyword']}\n"
            f"    ↳ {r['method']} {r['api_url'][:60]}"
        )
    return "\n".join(lines)


def _api_add(rest, creator):
    """解析：<关键词> <API地址> [--匹配 x] [--方法 x] [--参数 'json'] [--请求头 'json'] [--提取 path] [--前缀 text]"""
    tokens = rest.split()
    if len(tokens) < 2:
        return ("用法：/关键词api 添加 <关键词> <API地址> "
                "[--匹配 ...] [--方法 ...] [--参数 ...] [--请求头 ...] [--提取 ...] [--前缀 ...]")
    keyword = tokens[0]
    api_url = tokens[1]

    # 可选参数（--xxx 值 或 --xxx=值）
    opts = {"匹配": "exact", "方法": "GET", "参数": "", "请求头": "", "提取": "", "前缀": ""}
    i = 2
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            key = tok[2:].lstrip("-")
            if "=" in key:
                k, v = key.split("=", 1)
                opts.setdefault(k, v)
            else:
                opts[key] = tokens[i + 1] if i + 1 < len(tokens) else ""
                i += 1
        i += 1

    mt = (opts.get("匹配") or "exact").strip().lower()
    if mt not in engine.VALID_MATCH:
        return f"匹配方式无效: {mt}（可选 {', '.join(engine.VALID_MATCH)}）"
    method = (opts.get("方法") or "GET").upper()
    if method not in ("GET", "POST"):
        return f"方法无效: {method}（可选 GET/POST）"
    headers_tpl = opts.get("请求头") or opts.get("headers") or ""

    try:
        rid = engine.add_api_rule(keyword, mt, api_url, method,
                                  opts.get("参数") or "", headers_tpl,
                                  opts.get("提取") or "", opts.get("前缀") or "", creator)
        engine.sync_dynamic_commands()
        return (f"✅ 已添加规则 #{rid}\n"
                f"关键词：{keyword}（{mt}）\n"
                f"API：{api_url}（{method}）\n"
                f"提取路径：{opts.get('提取') or '自动'}\n"
                f"前缀：{opts.get('前缀') or '无'}\n"
                f"（已注册为动态命令，命令管理页可见）")
    except Exception as e:
        ctx.log(f"添加 API 规则失败: {e}", level="error")
        return f"❌ 添加失败: {e}"


def _api_delete(rest):
    rid = rest.strip()
    if not rid.isdigit():
        return "用法：/关键词api 删除 <id>"
    n = engine.delete_rule_by_id(rid)
    engine.sync_dynamic_commands()
    return f"✅ 已删除规则 #{rid}" if n else f"❌ 规则 #{rid} 不存在"


def _api_set_enabled(rest, enabled):
    rid = rest.strip()
    if not rid.isdigit():
        return "用法：/关键词api 启用|禁用 <id>"
    n = engine.set_rule_enabled_by_id(rid, enabled)
    engine.sync_dynamic_commands()
    if not n:
        return f"❌ 规则 #{rid} 不存在"
    return f"✅ 规则 #{rid} 已{'启用' if enabled else '禁用'}"


def _api_test(rest):
    """/关键词api 测试 <id> [内容]"""
    parts = rest.split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        return "用法：/关键词api 测试 <id> [内容]"
    rid = int(parts[0])
    text = parts[1].strip() if len(parts) > 1 else ""
    rule = engine.get_rule_by_id(rid)
    if rule is None:
        return f"❌ 规则 #{rid} 不存在"
    if rule["action_type"] != engine.ACTION_API:
        return f"❌ 规则 #{rid} 不是 API 动作规则"
    if not text:
        text = rule["keyword"]

    content = engine.call_api_sync(rule, text)
    if not content:
        return f"❌ 测试失败（规则 #{rid}「{rule['keyword']}」API 未返回可用内容）"
    max_len = int(_conf("max_reply_len", 2000) or 2000)
    reply = ((rule.get("prefix") or "") + content)[:max_len]
    return f"🧪 规则 #{rid} 测试结果（内容: {text}）:\n{reply}"


def _kwapi_dyn_handler(rule, message):
    """动态命令回调：消息未命中插件命令时由框架路由按规则匹配方式兜底调用"""
    return engine.kwapi_dyn_handler(rule, message)


# ================= 监控通知命令（超管） =================

async def handle_watch_add(event, match):
    kw = (match.group(1) if match else "").strip()
    if not kw:
        await _reply(event, "用法: /监控 <关键词>")
        return
    rid = await asyncio.to_thread(engine.add_notify_rule, kw, event.user_id)
    if rid:
        await _reply(event, f"已添加监控词「{kw}」")
    else:
        await _reply(event, f"监控词「{kw}」已存在")


async def handle_watch_remove(event, match):
    kw = (match.group(1) if match else "").strip()
    if not kw:
        await _reply(event, "用法: /取消监控 <关键词>")
        return
    n = await asyncio.to_thread(engine.remove_notify_rule, kw)
    if n:
        await _reply(event, f"已移除监控词「{kw}」")
    else:
        await _reply(event, f"未找到监控词「{kw}」")


async def handle_watch_list(event, match):
    kws = await asyncio.to_thread(engine.notify_keywords)
    if not kws:
        await _reply(event, "当前没有任何监控词")
        return
    await _reply(event, "🔍 监控词：" + "、".join(kws))


# ================= 原始消息处理（命中匹配与动作分发） =================

async def _dispatch_notify(raw_event, gid, uid, text, bot_name):
    """监控通知动作：命中监控词后私聊全部超级管理员（仅观察，不接管消息）"""
    if not _conf("watch_enable", True):
        return
    hits = engine.match_notify_rules(gid, text)
    if not hits:
        return
    sups = await asyncio.to_thread(engine.superuser_ids)
    if not sups:
        return
    notice = (f"🔔 关键词命中提醒\n群：{raw_event.get('group_id')}\n"
              f"用户：{raw_event.get('user_id')}\n"
              f"命中：{', '.join(r['keyword'] for r in hits)}\n"
              f"内容：{text[:200]}")
    for s in sups:
        try:
            await ctx.asend_msg(user_id=s, group_id=None, message=notice, bot=bot_name)
        except Exception:
            pass
    await asyncio.to_thread(engine.bump_hits, [r["id"] for r in hits])


async def on_raw(raw_event, bot_name):
    """原始消息接管点：收到普通群文本消息时依次处理监控通知与自动回复。

    返回 True = 接管（命中自动回复后停止事件传播）；
    其余情况一律返回 False 不拦截，让其他插件与路由继续处理。
    """
    try:
        if raw_event.get("post_type") != "message":
            return False
        if raw_event.get("message_type") != "group":
            return False
        gid = str(raw_event.get("group_id") or "")
        uid = str(raw_event.get("user_id") or "")
        if not gid or not uid:
            return False
        if uid == str(raw_event.get("self_id") or ""):
            return False  # 忽略机器人自己的消息
        text = engine.extract_text(raw_event.get("message", "")).strip()
        if not text or text.startswith("/"):
            return False  # 跳过空文本与命令消息
        if text.startswith(_CMD_PREFIXES):
            return False  # 管理命令交给命令流程

        # 回退会话：会话等待器不可用时，由本插件接管「等待回复内容」的下一条消息
        if _sw() is None and await _handle_pending(raw_event, uid, gid, text):
            return True

        # 监控通知：仅观察，不接管消息
        await _dispatch_notify(raw_event, gid, uid, text, bot_name)

        # 自动回复：命中即发送并接管（全局规则优先、本群在后）
        if _conf("enable", True):
            matched = engine.match_reply_rules(gid, text)
            if matched:
                for r in matched:
                    await _send_raw(raw_event, r.get("reply_content", ""))
                await asyncio.to_thread(engine.bump_hits, [r["id"] for r in matched])
                return True
        return False
    except Exception as e:
        ctx.log(f"原始消息处理异常: {e}", level="warning")
        return False


# ================= 插件注册 =================

def register(plugin_ctx):
    global ctx
    ctx = plugin_ctx
    engine.init(ctx)

    # 注意注册顺序：先长前缀后短前缀，避免前缀包含导致误匹配
    ctx.command("添加全局正则", handle_add_global_regex, priority=50,
                require_superuser=True, description="添加全局正则回复（超管）")
    ctx.command("添加全局", handle_add_global, priority=50,
                require_superuser=True, description="添加全局关键词回复（超管）")
    ctx.command("添加正则", handle_add_regex, priority=50,
                require_admin=True, description="添加本群正则回复（管理员/群主）")
    ctx.command("添加", handle_add, priority=50,
                require_admin=True, description="添加本群关键词回复（管理员/群主）")
    ctx.command("删除全局", handle_delete_global, priority=50,
                require_superuser=True, description="删除全局回复（超管）")
    ctx.command("删除", handle_delete, priority=50,
                require_admin=True, description="删除本群回复，含正则（管理员/群主）")
    ctx.command("启用", handle_enable, priority=50,
                require_admin=True, description="启用本群回复（管理员/群主）")
    ctx.command("禁用", handle_disable, priority=50,
                require_admin=True, description="禁用本群回复（管理员/群主）")
    ctx.command("回复帮助", handle_help, priority=50, description="查看关键词引擎帮助")
    ctx.command("查看回复", handle_list, priority=50, description="查看本群回复列表")
    ctx.command("查看全局回复", handle_list_global, priority=50,
                require_superuser=True, description="查看全局回复列表（超管）")
    ctx.command("回复统计", handle_stats, priority=50, description="查看回复数量与命中统计")

    # API 动作规则管理（超管）；规则统一注册为框架动态命令，
    # WebUI 命令管理 → 关键词回复 可见/启停，路由在插件命令未命中时兜底触发
    ctx.command("/关键词api", cmd_main, priority=40,
                require_superuser=True, alias=["/kwapi"],
                description="关键词API规则管理（超管）：添加/删除/列表/启用/禁用/测试")
    try:
        engine.sync_dynamic_commands()
    except Exception as e:
        ctx.log(f"初始同步动态命令失败: {e}", level="warning")

    # 监控通知（超管）
    ctx.command("^/监控列表\\s*$", handle_watch_list, priority=50,
                description="查看监控词", require_superuser=True)
    ctx.command("^/监控\\s+([\\s\\S]+)", handle_watch_add, priority=50,
                description="新增监控词", require_superuser=True)
    ctx.command("^/取消监控\\s+([\\s\\S]+)", handle_watch_remove, priority=50,
                description="移除监控词", require_superuser=True)

    ctx.on_raw_message(on_raw)
    ctx.task("*/5 * * * *", _cleanup_pending, description="清理过期的规则添加会话")
    ctx.log("关键词引擎插件已注册：添加/添加正则/添加全局/添加全局正则/删除/删除全局/"
            "启用/禁用/查看回复/查看全局回复/回复统计/回复帮助 + /关键词api + /监控")

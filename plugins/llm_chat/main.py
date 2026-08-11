"""
LLM 对话插件 v1.8.0
============================
通过 OpenAI 兼容接口提供 QQ 群/私聊内的大模型对话能力，并支持 AI 调用函数（Function Calling）。

功能：
  1. /chat <内容>           — 发起对话（自动带上下文记忆）
  2. /chat clear            — 清空当前会话上下文
  3. /chat new              — 新开一轮会话（清空上下文）
  4. 艾特机器人触发对话       — @机器人 + 内容 直接对话
  5. 黑名单联动              — 被 llm_blacklist 拉黑的用户拒绝对话
  6. AI 调用函数（工具）      — 其他插件可注册函数，AI 按需调用（function calling）
  7. 提示词注入              — 其他插件可注入 system prompt 片段
  8. 请求日志上报            — 每次请求记录 时间/群号/QQ/请求摘要/回复摘要
  9. 上下文追溯（存库）      — 对话历史写入 MySQL，AI 可主动追溯最近 1000 条
 10. 群消息/群身份能力      — AI 可获取群消息、群成员列表、成员身份；群上下文自动注入
 11. 好感度系统            — 每次对话 +1，AI 可读取/调整，/好感度 查看
 12. 长期记忆系统          — AI 可读/写/删记忆，重要记忆最多 5 条注入上下文
 13. 详细日志              — 全链路日志（请求/响应/usage/耗时/堆栈/缓存命中）
  14. 人格预设系统          — 人格预设系统：多人格可切换（/人格 指令）
  15. 对话统计 / 会话状态    — /对话统计、/会话、/函数列表 指令（对话统计）
  16. 开关体系              — 群聊/私聊/指令/艾特/人格 独立开关（WebUI 配置）
  17. 函数调用日志          — 每次 AI 工具调用记录 参数/结果/耗时/成败（WebUI 可视）
  18. 自研 WebUI            — 函数列表/调用日志/统计/人格/配置 一体化控制台（嵌入框架后台）

公共接口（供其他插件以「函数注入 + 提示词注入」方式扩展）：
  - register_llm_function(name, description, parameters, handler, plugin_name='')
  - unregister_llm_function(name)
  - register_llm_prompt(content, priority=0, plugin_name='')
  - get_llm_function_schemas()  /  get_llm_prompt_parts()  /  get_llm_identity_parts()
"""
import asyncio
import inspect
import ipaddress
import json
import os
import random
import re
import socket
import threading
import time
import traceback
import urllib.parse

__plugin_meta__ = {
    "name": "LLM 对话",
    "version": "1.8.0",
    "author": "ZGRIC",
    "desc": "QQ 内大模型对话（全异步，自动上下文记忆，AI 调用函数、人格预设、好感度、长期记忆、函数调用日志 + 自研 WebUI 控制台），用法: /chat <内容> 或直接 @机器人 说内容",
    "priority": 50,
}

# 内存中的会话上下文：user_id -> {"msgs": [...], "ts": 最后活跃时间}
_SESSIONS = {}
_SESSION_TTL = 3600  # 会话空闲 1 小时自动清理

# 框架上下文（由 register 注入，供全局函数使用）
ctx = None

# ================= 函数注册表（供其他插件注入） =================
_LLM_FUNCTIONS = {}
_LLM_FUNCTIONS_LOCK = threading.Lock()
_PROMPT_PARTS = []
_PROMPT_LOCK = threading.Lock()
_IDENTITY_PARTS = []
_IDENTITY_LOCK = threading.Lock()

# 对话历史表名（MySQL），每用户最多保留条数
_HISTORY_TABLE = "llm_chat_history"
_HISTORY_MAX_PER_USER = 1000
_PERSONA_TABLE = "llm_personas"  # 人格预设表

# OneBot API 缓存（避免每次对话重复拉群信息导致超时）
_API_CACHE = {}
_API_CACHE_LOCK = threading.Lock()
_API_CACHE_TTL = 60          # 群信息/成员信息缓存 60s
_API_CACHE_MAX = 500         # 缓存条目上限，超出整体清空防泄漏

MAX_TOOL_ROUNDS_DEFAULT = 5  # 单次对话最多工具调用轮数

# ===== WebUI 数据（函数调用日志 + 统计，内存环形缓冲） =====
_TOOL_LOGS = []            # 函数调用日志（最新在前）
_TOOL_LOGS_LOCK = threading.Lock()
_TOOL_LOGS_MAX_DEFAULT = 200

_STATS = {"chats": 0, "tool_calls": 0, "tool_errors": 0, "started_at": time.time()}
_STATS_LOCK = threading.Lock()


# ================= 日志工具（详细日志，方便排查） =================

def _log_info(msg):
    """info 级别日志（自动带 LLM 前缀）"""
    try:
        if ctx is not None:
            ctx.log(f"[LLM] {msg}", level="info")
    except Exception:
        pass


def _log_warn(msg):
    """warning 级别日志（自动带 LLM 前缀）"""
    try:
        if ctx is not None:
            ctx.log(f"[LLM] {msg}", level="warning")
    except Exception:
        pass


def _log_err(msg, exc=None):
    """error 级别日志，附带完整异常堆栈（排查必需）"""
    try:
        if exc:
            msg += f"\n{traceback.format_exc() if isinstance(exc, BaseException) else exc}"
        if ctx is not None:
            ctx.log(f"[LLM] {msg}", level="error")
    except Exception:
        pass


# ================= 公共注入接口 =================

def register_llm_function(name, description="", parameters=None, handler=None, plugin_name=""):
    """注册一个 AI 可调用的函数（工具）。handler 签名：def/async def (args) 或 (args, ctx, event, user_id)"""
    name = str(name or "").strip()
    if not name:
        raise ValueError("register_llm_function: name 不能为空")
    if not callable(handler):
        raise TypeError(f"register_llm_function: handler 不可调用 (name={name})")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    with _LLM_FUNCTIONS_LOCK:
        existed = name in _LLM_FUNCTIONS
        _LLM_FUNCTIONS[name] = {
            "description": str(description or ""),
            "parameters": parameters,
            "handler": handler,
            "plugin": str(plugin_name or ""),
        }
    _log_info(f"函数注册: {name} 插件={plugin_name or '未知'} {'(覆盖)' if existed else ''}")
    return True


def unregister_llm_function(name):
    """注销一个 AI 可调用函数。"""
    with _LLM_FUNCTIONS_LOCK:
        if name in _LLM_FUNCTIONS:
            del _LLM_FUNCTIONS[name]
            _log_info(f"函数注销: {name}")
            return True
    return False


def register_llm_prompt(content, priority=0, plugin_name=""):
    """注入一段 system prompt 片段（提示词注入）。priority 越大越靠前。"""
    content = str(content or "").strip()
    if not content:
        return False
    with _PROMPT_LOCK:
        _PROMPT_PARTS.append({
            "content": content,
            "priority": float(priority or 0),
            "plugin": str(plugin_name or ""),
        })
    _log_info(f"提示词注入: [{plugin_name or '未知'}] {content[:40]}")
    return True


def get_llm_function_schemas():
    """返回 OpenAI tools 格式的已注册函数列表。"""
    with _LLM_FUNCTIONS_LOCK:
        items = list(_LLM_FUNCTIONS.items())
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": item["description"],
                "parameters": item["parameters"],
            },
        }
        for name, item in items
    ]


def get_llm_prompt_parts():
    """返回按优先级排序的注入提示词片段列表。"""
    with _PROMPT_LOCK:
        parts = list(_PROMPT_PARTS)
    parts.sort(key=lambda p: -p["priority"])
    return [p["content"] for p in parts]


def register_llm_identity(content, plugin_name=""):
    """注入一段身份说明（并入群上下文，供 AI 识别交流对象身份）。"""
    content = str(content or "").strip()
    if not content:
        return False
    with _IDENTITY_LOCK:
        _IDENTITY_PARTS.append({
            "content": content,
            "plugin": str(plugin_name or ""),
        })
    _log_info(f"身份注入: [{plugin_name or '未知'}] {content[:40]}")
    return True


def get_llm_identity_parts():
    """返回所有已注入的身份说明片段列表。"""
    with _IDENTITY_LOCK:
        return [p["content"] for p in list(_IDENTITY_PARTS)]


# ================= 内部工具 =================

def register(ctx_arg):
    """插件注册入口"""
    global ctx
    ctx = ctx_arg
    # 建表（幂等）：对话历史 / 好感度 / 长期记忆
    _ensure_tables()
    ctx.command(
        "/chat", handle_chat,
        priority=50,
        alias=["/对话", "/ai聊天", "/gpt"],
        description="与 LLM 对话（自动记忆上下文），用法: /chat <内容>；/chat clear 清空上下文",
    )
    ctx.command(
        "^\\[@\\d+\\]", handle_at,
        priority=50,
        description="艾特机器人触发 LLM 对话（自动记忆上下文）",
    )
    ctx.command(
        "/好感度", handle_affinity,
        priority=50,
        description="查看自己的好感度与等级",
    )
    ctx.command(
        "/记忆", handle_memory,
        priority=50,
        description="查看自己的长期记忆列表",
    )
    # 超管命令：直接管理检测 API 池（不依赖 AI 工具调用）
    ctx.command(
        "/检测api", handle_detect_api,
        priority=50,
        require_superuser=True,
        description="超管命令：管理内容检测 API 池。用法: /检测api list | add <名称> <URL> | remove <名称> | enable <名称> | disable <名称>",
    )
    # 提示词注入：明确告知 AI 拥有管理检测 API 池的能力（支持工具调用的模型会主动调用 manage_detect_api）
    register_llm_prompt(
        "你可以使用 manage_detect_api 工具来管理「内容检测 API 池」（图片 NSFW/违禁内容检测接口的增删查）。"
        "当用户要求添加/更换/查看图片检测接口时，主动调用它；"
        "用法: add(name,url) 添加、list 查看、remove(name) 删除、enable(name)/disable(name) 启用禁用。",
        priority=100,
        plugin_name="llm_chat",
    )
    # 注册内置 AI 可调用函数
    register_llm_function(
        name="get_chat_history",
        description="追溯指定用户的 LLM 对话历史（来自数据库，最多 1000 条）。需要回忆某人之前说过什么时调用。",
        parameters={"type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "目标用户 QQ 号，缺省用当前对话人"},
                        "limit": {"type": "integer", "description": "返回条数，1-200，默认 20"},
                    },
                    "required": []},
        handler=_fn_get_chat_history,
        plugin_name="llm_chat",
    )
    register_llm_function(
        name="get_group_msgs",
        description="获取指定 QQ 群的最近聊天消息（含群号、发送者昵称、时间）。需要了解群里最近聊了什么时调用。",
        parameters={"type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "QQ 群号，缺省用当前群"},
                        "limit": {"type": "integer", "description": "返回条数，1-100，默认 20"},
                    },
                    "required": []},
        handler=_fn_get_group_msgs,
        plugin_name="llm_chat",
    )
    register_llm_function(
        name="get_group_members",
        description="获取指定 QQ 群的成员列表（含昵称、QQ、群内角色，一次最多返回 200 条）。需要知道群里有谁时调用。",
        parameters={"type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "QQ 群号，缺省用当前群"},
                    },
                    "required": []},
        handler=_fn_get_group_members,
        plugin_name="llm_chat",
    )
    register_llm_function(
        name="get_member_profile",
        description="查询某人在指定群内的身份（角色/昵称/专属头衔，含插件注入的身份说明）。需要知道某人身份时调用。",
        parameters={"type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "QQ 群号，缺省用当前群"},
                        "user_id": {"type": "string", "description": "目标 QQ 号"},
                    },
                    "required": ["user_id"]},
        handler=_fn_get_member_profile,
        plugin_name="llm_chat",
    )
    # 私聊记录函数（NapCat 实时拉取，不落库）
    register_llm_function(
        name="get_private_msgs",
        description="获取与指定好友的最近私聊消息（真实QQ私聊记录，NapCat接口实时拉取，含对方昵称/时间/发送方向）。需要回忆与某人私聊说过什么时调用。",
        parameters={"type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "目标好友 QQ 号"},
                        "limit": {"type": "integer", "description": "返回条数，1-200，默认 20"},
                    },
                    "required": ["user_id"]},
        handler=_fn_get_private_msgs,
        plugin_name="llm_chat",
    )
    # 好感度函数
    register_llm_function(
        name="get_affinity",
        description="读取指定用户对你的好感度数值与等级。想知道自己与某人关系如何时调用。",
        parameters={"type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "目标用户 QQ 号，缺省用当前对话人"},
                    },
                    "required": []},
        handler=_fn_get_affinity,
        plugin_name="llm_chat",
    )
    register_llm_function(
        name="change_affinity",
        description="调整指定用户对你的好感度（单次 ±5 以内，用于奖励/惩罚用户行为）。",
        parameters={"type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "目标用户 QQ 号"},
                        "delta": {"type": "integer", "description": "好感度变化值，-5 到 +5，正为增加负为减少"},
                        "reason": {"type": "string", "description": "调整原因（记录日志用）"},
                    },
                    "required": ["user_id", "delta"]},
        handler=_fn_change_affinity,
        plugin_name="llm_chat",
    )
    # 长期记忆函数
    register_llm_function(
        name="memory_write",
        description="为用户写入一条长期记忆（跨会话持久保存）。重要的事情（用户喜好/约定/承诺）可设 important=1，会注入后续对话上下文。",
        parameters={"type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "目标用户 QQ 号，缺省用当前对话人"},
                        "content": {"type": "string", "description": "记忆内容，尽量简洁，最多 500 字"},
                        "important": {"type": "integer", "description": "是否重要记忆：1=重要(会注入上下文) 0=普通(默认)"},
                    },
                    "required": ["content"]},
        handler=_fn_memory_write,
        plugin_name="llm_chat",
    )
    register_llm_function(
        name="memory_read",
        description="读取用户的长期记忆，可按关键词过滤。需要回忆关于某用户的已记录信息时调用。",
        parameters={"type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "目标用户 QQ 号，缺省用当前对话人"},
                        "keyword": {"type": "string", "description": "关键词，按内容模糊匹配，可留空"},
                        "limit": {"type": "integer", "description": "返回条数，1-50，默认 20"},
                    },
                    "required": []},
        handler=_fn_memory_read,
        plugin_name="llm_chat",
    )
    register_llm_function(
        name="memory_delete",
        description="删除用户的一条长期记忆（按记忆 id）。需要遗忘某条记录时调用。",
        parameters={"type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "目标用户 QQ 号，缺省用当前对话人"},
                        "id": {"type": "integer", "description": "要删除的记忆 id"},
                    },
                    "required": ["id"]},
        handler=_fn_memory_delete,
        plugin_name="llm_chat",
    )
    # 内容安全检测函数（AI 可主动自查 / 动态增删检测 API）
    register_llm_function(
        name="detect_content",
        description="检测文本或图片链接是否包含违禁内容（违禁词/色情/NSFW 等）。发消息给用户前可主动自查，或不确定某内容是否违规时调用。返回通过/未通过及原因。",
        parameters={"type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要检测的文本内容（含图片链接也可）"},
                        "image_url": {"type": "string", "description": "要检测的图片 URL（可选）"},
                    },
                    "required": ["text"]},
        handler=_fn_detect_content,
        plugin_name="llm_chat",
    )
    register_llm_function(
        name="manage_detect_api",
        description="管理内容检测 API 端点（图片 NSFW 检测接口，增删查）。action: list 查看 / add(name,url) 添加 / remove(name) 删除 / enable(name) 启用 / disable(name) 禁用。用于扩充或更换图片检测接口。",
        parameters={"type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "list/add/comment/remove/enable/disable"},
                        "name": {"type": "string", "description": "端点名称"},
                        "url": {"type": "string", "description": "端点地址（add 时必填）"},
                    },
                    "required": ["action"]},
        handler=_fn_manage_detect_api,
        plugin_name="llm_chat",
    )
    # ===== 通用 API 池：AI 可自主添加/调用任意 HTTP(S) 接口 =====
    register_llm_function(
        name="manage_api_pool",
        description="管理「通用 API 池」中的 API 节点（增删查启停/写注释任意 HTTP(S) 接口）。action: list 查看 / add(name,url,method,headers,note) 添加（系统会自动探测该接口返回结构并写入 note）/ comment(name,note) 写或改注释 / remove(name) 删除 / enable(name) 启用 / disable(name) 禁用。用于扩充 AI 可调用的数据源（天气/快递/状态查询等）。",
        parameters={"type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "list/add/remove/enable/disable"},
                        "name": {"type": "string", "description": "节点名称"},
                        "url": {"type": "string", "description": "接口地址（add 时必填）"},
                        "method": {"type": "string", "description": "请求方法 GET/POST（add 时可选，默认 GET）"},
                        "headers": {"type": "string", "description": "自定义请求头 JSON 字符串，如 {\"Authorization\":\"Bearer xxx\"}（add 时可选）"},
                        "note": {"type": "string", "description": "节点注释：用途/返回结构/鉴权方式等（add 或 comment 时使用；add 时系统自动探测返回结构并合并写入）"},
                    },
                    "required": ["action"]},
        handler=_fn_manage_api_pool,
        plugin_name="llm_chat",
    )
    register_llm_function(
        name="call_api",
        description="调用「通用 API 池」中的节点或直接请求任意 HTTP(S) 接口，返回 JSON/文本结果供你分析。name 传 API 池节点名（优先）；也可直接传 url 请求任意公网接口。支持 GET/POST、JSON body、URL 参数、自定义 headers。拿到返回数据后由你自由解读、总结、播报给用户。",
        parameters={"type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "API 池节点名称（优先于 url）"},
                        "url": {"type": "string", "description": "完整接口地址（name 为空时必填，可带 URL 参数）"},
                        "method": {"type": "string", "description": "GET 或 POST，默认 GET"},
                        "params": {"type": "object", "description": "附加 URL 查询参数（可选），如 {\"city\":\"上海\"}"},
                        "json_body": {"type": "object", "description": "POST 时发送的 JSON body（可选）"},
                        "headers": {"type": "object", "description": "本次请求额外 headers（可选），如 {\"Authorization\":\"Bearer xxx\"}"},
                        "timeout": {"type": "integer", "description": "超时秒数，默认 15"},
                    },
                    "required": []},
        handler=_fn_call_api,
        plugin_name="llm_chat",
    )
    register_llm_prompt(
        "你可以用 manage_api_pool 维护「通用 API 池」，并用 call_api 调用池内节点或任意公网 HTTP(S) 接口获取 JSON 数据，"
        "然后自由分析、总结、播报这些数据。例如用户要查天气/快递/服务器状态/某个网站信息时，先调 call_api 拿到数据再回答；"
        "用户给出一个 API 地址要求接入时，用 manage_api_pool 的 add(name,url,note) 添加到池中。"
        "【硬性要求·写入时】每次 add 添加 API 节点时，系统会自动探测该接口的返回结构并写入该节点的 note（注释）；"
        "你应当查看返回的结构探测结果，并主动用 comment(name,note) 补充完善注释，写明该接口的用途、返回字段含义、鉴权方式等，方便以后复用。"
        "【主动推荐】平时对话中若用户提到相关需求（如查天气、查快递、查服务器状态、查网站信息等），"
        "先 manage_api_pool list 查看池中是否有现成可用的节点（看 name 和 note 判断用途），"
        "有就直接 call_api 调用并帮用户处理；没有合适的再考虑新接入。",
        priority=95,
        plugin_name="llm_chat",
    )
    ctx.command(
        "/api池", handle_api_pool,
        priority=50,
        require_superuser=True,
        description="超管命令：管理通用 API 池。用法: /api池 list | add <名称> <URL> [GET|POST] | note <名称> <注释> | remove <名称> | enable <名称> | disable <名称>",
    )
    # 人格管理 / 统计 / 会话状态 / 函数列表
    ctx.command(
        "/人格", handle_persona,
        priority=50,
        alias=["/persona", "/人设"],
        description="人格预设管理。用法: /人格 list | use <名称> | off | add <名称> <描述> | del <名称>",
    )
    ctx.command(
        "/对话统计", handle_stats,
        priority=50,
        description="查看 LLM 对话统计（会话数/工具调用/今日对话）",
    )
    ctx.command(
        "/会话", handle_session,
        priority=50,
        description="查看当前会话上下文状态",
    )
    ctx.command(
        "/函数列表", handle_fn_list,
        priority=50,
        description="查看 LLM 当前可调用的函数列表",
    )
    # 仪表盘卡片（WebUI 首页统计）
    try:
        ctx.dashboard_card(
            title="LLM 对话",
            icon="🤖",
            priority=30,
            handler=_dashboard_card_data,
        )
    except Exception as e:
        _log_warn(f"注册仪表盘卡片失败: {e}")
    # 初始写一次 WebUI 数据快照（函数列表/统计/人格）
    _write_webui_snapshot()
    # 注册插件 WebUI（嵌入框架管理后台）
    try:
        ctx.webui(
            title="LLM 对话",
            entry="index.html",
            icon="🤖",
            order=30,
        )
        _log_info("WebUI 注册成功: LLM 对话")
    except Exception as e:
        _log_warn(f"WebUI 注册失败: {e}")
    _log_info(f"注册完成: /chat /好感度 /记忆 /人格 /对话统计 /会话 /函数列表 + 内置AI函数 {len(get_llm_function_schemas())} 个 "
              f"(历史追溯/群消息/群成员/成员身份/好感度×2/记忆×3)")


# ================= 配置读取 =================

def _get_config(key, default=None):
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


def _cfg_bool(key, default=False):
    """读取布尔配置（兼容字符串/布尔/数字）"""
    v = _get_config(key, default)
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes", "on", "是", "开")


def _set_config_value(key, value):
    """写入插件配置（UPSERT，不依赖表唯一约束，双数据库兼容）"""
    sv = json.dumps(value, ensure_ascii=False)
    try:
        row = ctx.db_query_one(
            "SELECT id FROM plugin_configs WHERE plugin_name='llm_chat' AND config_key=%s", (key,))
        if row:
            ctx.db_execute("UPDATE plugin_configs SET config_value=%s WHERE id=%s", (sv, row["id"]))
        else:
            ctx.db_execute(
                "INSERT INTO plugin_configs (plugin_name, config_key, config_value) "
                "VALUES ('llm_chat', %s, %s)", (key, sv))
    except Exception as e:
        _log_warn(f"写入配置 {key} 失败: {e}")


def _default_prompt() -> str:
    base = str(_get_config("system_prompt", "")).strip() or (
        "你是 ZCBOT OneBot QQ 机器人框架上的一位乐于助人的 AI 助手，"
        "回答简洁、准确、友好，使用与用户相同的语言。"
    )
    # 人格预设注入（多人格切换）
    persona = _persona_prompt()
    if persona:
        base = persona + "\n\n" + base
    parts = get_llm_prompt_parts()
    if parts:
        base += "\n\n【插件注入的能力说明】\n" + "\n".join(f"- {p}" for p in parts)
    # 分段回复规则注入：AI 想分段就在段间放占位符，框架自动拆段延迟发送
    placeholder = str(_get_config("segment_placeholder", "<dvi>") or "<dvi>")
    if placeholder:
        base += (f"\n\n【分段回复规则】若想把回复分成多段逐条发送（更像真人），"
                 f"请在段与段之间插入占位符 {placeholder}，框架会自动拆段并按 "
                 f"{_get_config('segment_delay_min', 1)}~{_get_config('segment_delay_max', 3)} 秒间隔逐条发送。"
                 f"不需要分段就不要使用该占位符。")
    return base


def _model() -> str:
    return str(_get_config("model", "gpt-4o-mini")).strip() or "gpt-4o-mini"


def _base_url() -> str:
    return str(_get_config("base_url", "")).strip().rstrip('/')


def _max_tool_rounds() -> int:
    try:
        v = int(_get_config("max_tool_rounds", MAX_TOOL_ROUNDS_DEFAULT) or MAX_TOOL_ROUNDS_DEFAULT)
        return max(1, min(v, 10))
    except Exception:
        return MAX_TOOL_ROUNDS_DEFAULT


# ================= 数据库建表 =================

def _ensure_column_type(table: str, column: str = "user_id", new_type: str = "VARCHAR(64)"):
    """修复已存在旧表里的 TEXT/BLOB 键列（MySQL 下 TEXT 建索引报 1170）。

    CREATE TABLE IF NOT EXISTS 不会改已有表结构，早期版本把 user_id 建成
    TEXT，升级后索引就建不上去。检测到 TEXT/BLOB 才 ALTER，幂等无副作用；
    SQLite 无此限制，SHOW COLUMNS 不支持时静默跳过。
    """
    try:
        rows = ctx.db_query(f"SHOW COLUMNS FROM {table} LIKE '{column}'", ())
    except Exception:
        return  # SQLite 或驱动不支持 SHOW COLUMNS → 无此问题，跳过
    if not rows:
        return
    r0 = rows[0]
    if isinstance(r0, dict):
        typ = str(r0.get("Type") or r0.get("type") or "").lower()
    else:
        typ = str(r0[1] if len(r0) > 1 else "").lower()
    if 'text' not in typ and 'blob' not in typ:
        return  # 已是 VARCHAR/INT 等，无需修复
    try:
        ctx.db_execute(
            f"ALTER TABLE {table} MODIFY COLUMN {column} {new_type} NOT NULL", ())
        _log_info(f"列类型修复 {table}.{column}: {typ} → {new_type} NOT NULL")
    except Exception as e:
        _log_err(f"列类型修复失败 {table}.{column}: {e}")


def _ensure_column(table: str, column: str, definition: str):
    """确保列存在（幂等）：缺失则 ALTER TABLE ADD COLUMN，SQLite/MySQL 双兼容。

    CREATE TABLE IF NOT EXISTS 不会改已有表结构，升级后旧表缺少新列时用它补齐。
    """
    try:
        rows = ctx.db_query(f"PRAGMA table_info({table})", ())
        if rows:
            cols = set()
            for r in rows:
                if isinstance(r, dict):
                    cols.add(str(r.get("name") or ""))
                elif len(r) > 1:
                    cols.add(str(r[1]))
            if column in cols:
                return
    except Exception:
        pass  # MySQL 不支持 PRAGMA → 走 SHOW COLUMNS 尝试
    try:
        rows = ctx.db_query(f"SHOW COLUMNS FROM {table} LIKE '{column}'", ())
        if rows:
            return
    except Exception:
        pass
    try:
        ctx.db_execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}", ())
        _log_info(f"已为 {table} 补齐列 {column} {definition}")
    except Exception as e:
        _log_err(f"补齐列失败 {table}.{column}: {e}")


def _ensure_index(table: str, idx_name: str, columns: str):
    """创建索引，MySQL/SQLite 双兼容（幂等）。

    - SQLite 支持 CREATE INDEX IF NOT EXISTS，直接成功
    - MySQL 不支持 IF NOT EXISTS（报 1064 语法错误）→ 降级为普通
      CREATE INDEX，若已存在则报 1061 Duplicate key name → 忽略
    """
    if not idx_name or not columns:
        return
    ddl_if = f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({columns})"
    try:
        ctx.db_execute(ddl_if, ())
        _log_info(f"索引就绪 {table}.{idx_name}（SQLite 语法）")
        return
    except Exception as e:
        msg = str(e).lower()
    # MySQL：TEXT/BLOB 列不能建索引（1170）→ 修复列类型后重试一次
    if '1170' in msg or 'key length' in msg or 'blob/text' in msg:
        _ensure_column_type(table, 'user_id', 'VARCHAR(64)')
        try:
            ctx.db_execute(f"CREATE INDEX {idx_name} ON {table} ({columns})", ())
            _log_info(f"索引就绪 {table}.{idx_name}（列类型修复后）")
        except Exception as e2:
            msg2 = str(e2).lower()
            if 'duplicate' in msg2 or '1061' in msg2 or 'already exists' in msg2:
                _log_info(f"索引已存在 {table}.{idx_name}，跳过")
            else:
                _log_err(f"建索引失败 {table}.{idx_name}: {e2}")
        return
    # MySQL：IF NOT EXISTS 语法不支持 → 普通建索引
    if '1064' in msg or 'syntax' in msg or 'if not exists' in msg:
        try:
            ctx.db_execute(f"CREATE INDEX {idx_name} ON {table} ({columns})", ())
            _log_info(f"索引就绪 {table}.{idx_name}（MySQL 模式）")
        except Exception as e2:
            msg2 = str(e2).lower()
            if 'duplicate' in msg2 or '1061' in msg2 or 'already exists' in msg2:
                _log_info(f"索引已存在 {table}.{idx_name}，跳过")
            else:
                _log_err(f"建索引失败 {table}.{idx_name}: {e2}")
        return
    # 已存在的索引（部分驱动直接报错）
    if 'duplicate' in msg or '1061' in msg or 'already exists' in msg:
        _log_info(f"索引已存在 {table}.{idx_name}，跳过")
        return
    _log_err(f"建索引失败 {table}.{idx_name}: {e}")


def _ensure_tables():
    """建表 + 索引（幂等；MySQL/SQLite 双兼容）。

    注意：DDL 必须写成 MySQL 优先语法，框架在 SQLite 模式下会自动翻译：
    - 主键/索引列用 VARCHAR(n)（MySQL 下 TEXT 做键会报 1170）
    - TEXT 列不能带 DEFAULT（MySQL 下报 1101）
    - 自增列保留 AUTOINCREMENT，框架 MySQL 模式翻译为 AUTO_INCREMENT
    """
    ddl_list = [
        ("llm_chat_history",
         "CREATE TABLE IF NOT EXISTS llm_chat_history ("
         "id INTEGER PRIMARY KEY AUTOINCREMENT, "
         "user_id VARCHAR(64) NOT NULL, "
         "group_id VARCHAR(64) NOT NULL DEFAULT '', "
         "role VARCHAR(16) NOT NULL, "
         "content TEXT, "
         "ts INTEGER DEFAULT 0)",
         ("idx_llm_chat_history_user", "user_id, ts")),
        ("llm_affinity",
         "CREATE TABLE IF NOT EXISTS llm_affinity ("
         "user_id VARCHAR(64) PRIMARY KEY, "
         "affinity INTEGER DEFAULT 0, "
         "updated_at INTEGER DEFAULT 0)",
         None),
        ("llm_memory",
         "CREATE TABLE IF NOT EXISTS llm_memory ("
         "id INTEGER PRIMARY KEY AUTOINCREMENT, "
         "user_id VARCHAR(64) NOT NULL, "
         "content TEXT, "
         "important INTEGER DEFAULT 0, "
         "created_at INTEGER DEFAULT 0, "
         "updated_at INTEGER DEFAULT 0)",
         ("idx_llm_memory_user", "user_id, important")),
         # 人格预设（多人格可切换）
         ("llm_personas",
         "CREATE TABLE IF NOT EXISTS llm_personas ("
         "id INTEGER PRIMARY KEY AUTOINCREMENT, "
         "name VARCHAR(64) NOT NULL, "
         "prompt TEXT, "
         "enabled INTEGER DEFAULT 1, "
         "created_at INTEGER DEFAULT 0)",
         None),
         # 通用 API 池（AI 可自主增删节点，并用 call_api 自由调用任意 HTTP 接口）
         ("llm_api_pool",
          "CREATE TABLE IF NOT EXISTS llm_api_pool ("
          "id INTEGER PRIMARY KEY AUTOINCREMENT, "
          "name VARCHAR(64) NOT NULL, "
          "url VARCHAR(255) NOT NULL, "
          "method VARCHAR(8) DEFAULT 'GET', "
          "headers TEXT, "
            "note TEXT, "
          "enabled INTEGER DEFAULT 1, "
          "created_at INTEGER DEFAULT 0)",
          None),
          # 内容检测 API 列表（LLM 可动态增删，检测文本/图片时逐个尝试）
          ("llm_detect_api",
          "CREATE TABLE IF NOT EXISTS llm_detect_api ("
          "id INTEGER PRIMARY KEY AUTOINCREMENT, "
          "name VARCHAR(64) NOT NULL, "
          "url VARCHAR(255) NOT NULL, "
          "enabled INTEGER DEFAULT 1, "
          "created_at INTEGER DEFAULT 0)",
          None),
    ]
    ok_tables = []
    for table, ddl, idx in ddl_list:
        try:
            ctx.db_execute(ddl, ())
        except Exception as e:
            _log_err(f"建表失败 {table}（请检查 DDL 方言）: {e}")
            continue
        if idx:
            _ensure_column_type(table, 'user_id', 'VARCHAR(64)')
            _ensure_index(table, idx[0], idx[1])
        # 建表后立即验证，避免"运行时才发现表不存在"
        try:
            ctx.db_query_one(f"SELECT 1 FROM {table} LIMIT 1")
            ok_tables.append(table)
        except Exception as e:
            _log_err(f"建表后验证失败 {table}: {e}")
    if ok_tables:
        _log_info(f"数据表就绪并验证通过: {', '.join(ok_tables)}")
    else:
        _log_err("所有数据表均未就绪，请检查数据库配置与 DDL 方言")
    # 兼容旧表：为通用 API 池补齐注释列（新增字段，旧库自动迁移）
    _ensure_column('llm_api_pool', 'note', 'TEXT')


# ================= 内容检测（违禁词 + 图片 NSFW） =================

# 内置违禁词库（精简版；可在配置 detect_extra_words 追加）
_BUILTIN_BAN_WORDS = [
    "色情", "裸聊", "约炮", "援交", "招嫖", "卖淫", "嫖娼", "一夜情",
    "代开发票", "办假证", "假币", "赌博", "博彩", "六合彩", "澳门赌场",
    "毒品", "冰毒", "海洛因", "摇头丸", "大麻", "制毒", "迷药", "春药", "催情",
    "枪支", "弹药", "炸药", "炸弹", "传销", "诈骗", "洗钱", "套路贷", "高利贷",
    "裸贷", "跑分", "杀猪盘", "电信诈骗", "黑客攻击", "木马", "病毒源码",
    "撞库", "社工库", "银行卡四件套", "器官买卖", "代孕", "卖肾",
]

_IMG_EXT_RE = re.compile(
    r'https?://[^\s"\'\u300a\u300b<>()\[\]]+\.(?:jpg|jpeg|png|gif|webp|bmp)'
    r'(?:\?[^\s"\'\u300a\u300b<>()\[\]]*)?', re.I)
_MD_IMG_RE = re.compile(r'!\[[^\]]*\]\((https?://[^)\s]+)\)', re.I)

# 检测结果内存缓存（同一内容 60s 内不重复检测，避免图片重复请求 API）
_DETECT_CACHE = {}
_DETECT_CACHE_LOCK = threading.Lock()
_DETECT_CACHE_TTL = 60


def _detect_words() -> list:
    """内置词库 + 配置追加词库（去重）"""
    words = list(_BUILTIN_BAN_WORDS)
    extra = str(_get_config("detect_extra_words", "") or "").strip()
    if extra:
        for w in re.split(r"[,\s，、]+", extra):
            w = w.strip()
            if w and w not in words:
                words.append(w)
    return words


def _check_text(text: str):
    """违禁词检测：返回 (是否违规, 命中词列表)"""
    text = str(text or "")
    if not text:
        return False, []
    hits = []
    for w in _detect_words():
        if w and w in text:
            hits.append(w)
    return bool(hits), hits


def _extract_image_urls(text: str) -> list:
    """从文本中提取图片 URL（markdown 图片语法 + 裸链接图片后缀）"""
    urls = []
    for m in _MD_IMG_RE.finditer(text):
        u = m.group(1).strip()
        if u and u not in urls:
            urls.append(u)
    for m in _IMG_EXT_RE.finditer(text):
        u = m.group(0).rstrip('.,;:!?）)]')
        if u and u not in urls:
            urls.append(u)
    return urls


def _detect_api_endpoints() -> list:
    """图片检测端点：数据库 llm_detect_api（enabled=1）+ 配置 detect_image_urls + 默认 uapi 兜底"""
    eps = []
    try:
        rows = ctx.db_query("SELECT name, url FROM llm_detect_api WHERE enabled=1 ORDER BY id", ())
        for r in rows:
            eps.append((r.get("name") or r.get("url"), r.get("url") or ""))
    except Exception:
        pass
    cfg = str(_get_config("detect_image_urls", "") or "").strip()
    if cfg:
        for u in re.split(r"[,\s，、]+", cfg):
            u = u.strip()
            if u and not any(u == url for _, url in eps):
                eps.append(("config", u))
    # 始终追加 uapi NSFW 默认端点作为兜底（免 token，无需配置即可用）
    if not any("uapis.cn" in url for _, url in eps):
        eps.append(("uapi-nsfw(默认)", "https://uapis.cn/api/image/nsfw"))
    return eps


_NSFW_BAD = ("porn", "nsfw", "hentai", "sexy", "racy", "unsafe", "sensitive",
             "adult", "explicit", "dangerous", "porno")
_NSFW_GOOD = ("normal", "safe", "clean", "pure", "general", "ok", "pass",
              "allowed", "benign")


def _nsfw_verdict(label: str) -> bool:
    """按标签判断是否违规（宽松匹配：命中违规词 → 违规）"""
    lb = str(label or "").strip().lower()
    if not lb:
        return False
    if any(b in lb for b in _NSFW_BAD):
        return True
    return False


async def _check_image_url(url: str):
    """调用图片 NSFW 检测 API 检测单张图片：返回 (是否违规, 说明)"""
    url = str(url or "").strip()
    if not url:
        return False, ""
    import httpx
    eps = _detect_api_endpoints()
    if not eps:
        return False, "未配置检测端点"
    last_err = ""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        for name, ep in eps:
            try:
                if "{" in ep:
                    # 支持占位符端点：{url} / {image}
                    ep2 = ep.replace("{url}", url).replace("{image}", url)
                    resp = await client.get(ep2)
                else:
                    # 先 POST 表单（uapi 等多数接口用表单），非 2xx 再试 JSON
                    resp = await client.post(ep, data={"url": url})
                    if resp.status_code not in (200, 201):
                        resp = await client.post(ep, json={"url": url})
                if resp.status_code not in (200, 201):
                    last_err = f"{name}:HTTP{resp.status_code}"
                    continue
                try:
                    data = resp.json()
                except Exception:
                    last_err = f"{name}:非JSON"
                    continue
                if not isinstance(data, dict):
                    last_err = f"{name}:结构异常"
                    continue
                inner = data.get("data") if isinstance(data.get("data"), dict) else None
                label = None
                score = None
                if inner:
                    label = (inner.get("label") or inner.get("rating") or inner.get("result")
                             or inner.get("class") or inner.get("type") or inner.get("status"))
                    score = (inner.get("score") or inner.get("probability")
                             or inner.get("confidence"))
                if label is None:
                    label = (data.get("label") or data.get("rating") or data.get("result")
                             or data.get("class") or data.get("type") or data.get("status"))
                    score = (data.get("score") or data.get("probability")
                             or data.get("confidence"))
                if label is None:
                    label = str(data)
                verdict = _nsfw_verdict(str(label))
                if score is not None:
                    try:
                        if float(score) >= 0.8:
                            verdict = True
                    except (TypeError, ValueError):
                        pass
                return verdict, f"{name}:{label}"
            except Exception as e:
                last_err = f"{name}:{type(e).__name__}"
                continue
    return False, f"端点失败({last_err})"


async def _check_content(text: str):
    """综合检测：违禁词 + 图片。返回 dict{ok, reason, hits, images}"""
    text = str(text or "")
    import hashlib
    key = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
    now = time.time()
    with _DETECT_CACHE_LOCK:
        cached = _DETECT_CACHE.get(key)
        if cached and now - cached[0] < _DETECT_CACHE_TTL:
            return cached[1]
    bad, hits = _check_text(text)
    img_results = []
    if not bad:
        urls = _extract_image_urls(text)
        for u in urls[:5]:  # 单次最多检测 5 张
            img_bad, desc = await _check_image_url(u)
            img_results.append({"url": u, "bad": img_bad, "desc": desc})
            if img_bad:
                bad = True
                break
    if bad:
        reason = "包含违禁词"
        if hits:
            reason = "包含违禁词: " + "、".join(hits[:10])
        elif img_results and any(i["bad"] for i in img_results):
            badimg = next((i for i in img_results if i["bad"]), None)
            reason = f"图片内容违规({badimg['desc'] if badimg else 'NSFW'})"
        result = {"ok": False, "reason": reason, "hits": hits, "images": img_results}
    else:
        result = {"ok": True, "reason": "", "hits": [], "images": img_results}
    with _DETECT_CACHE_LOCK:
        _DETECT_CACHE[key] = (now, result)
        if len(_DETECT_CACHE) > 2000:
            _DETECT_CACHE.clear()
    return result


def _detect_enabled() -> bool:
    return str(_get_config("detect_enabled", True)).lower() in ("true", "1", "yes", "on")


def _detect_on_write() -> bool:
    return (_detect_enabled() and
            str(_get_config("detect_on_write", True)).lower() in ("true", "1", "yes", "on"))


def _detect_on_send() -> bool:
    return (_detect_enabled() and
            str(_get_config("detect_on_send", True)).lower() in ("true", "1", "yes", "on"))


def _detect_block_tip(default="⚠️ 内容未通过安全检测，已拦截。") -> str:
    return str(_get_config("detect_block_tip", default) or default)


async def _fn_detect_content(args, ctx=None, event=None, user_id=None):
    """AI 函数：检测文本/图片是否违规"""
    text = str(args.get("text") or "").strip()
    img = str(args.get("image_url") or "").strip()
    if img and not text:
        text = img
    if not text:
        return "错误：缺少 text 参数"
    try:
        r = await _check_content(text)
    except Exception as e:
        return f"检测异常：{e}"
    if r["ok"]:
        return "检测通过：内容正常（无违禁词，图片无违规）"
    return f"检测未通过：{r['reason']}"


async def _fn_manage_detect_api(args, ctx=None, event=None, user_id=None):
    """AI 函数：增删查检测 API 端点（存 llm_detect_api 表）"""
    action = str(args.get("action") or "list").strip().lower()
    name = str(args.get("name") or "").strip()
    url = str(args.get("url") or "").strip()
    try:
        if action in ("list", "查看"):
            rows = ctx.db_query("SELECT id, name, url, enabled FROM llm_detect_api ORDER BY id", ())
            if not rows:
                return "当前无自定义检测 API 端点（默认走配置 detect_image_urls 中的端点）"
            return "\n".join(f"[{r['id']}] {r['name']} {r['url']} enabled={r['enabled']}"
                             for r in rows)
        if action in ("add", "新增", "添加"):
            if not name or not url:
                return "错误：add 需要 name 和 url"
            ctx.db_execute(
                "INSERT INTO llm_detect_api (name, url, enabled, created_at) "
                "VALUES (%s, %s, 1, %s)",
                (name, url, int(time.time())))
            return f"已添加检测 API：{name} → {url}"
        if action in ("remove", "del", "delete", "删除"):
            if not name:
                return "错误：remove 需要 name（或 id）"
            ctx.db_execute("DELETE FROM llm_detect_api WHERE name=%s OR id=%s",
                           (name, name if name.isdigit() else -1))
            return f"已删除检测 API：{name}"
        if action in ("enable", "启用"):
            ctx.db_execute("UPDATE llm_detect_api SET enabled=1 WHERE name=%s", (name,))
            return f"已启用：{name}"
        if action in ("disable", "禁用"):
            ctx.db_execute("UPDATE llm_detect_api SET enabled=0 WHERE name=%s", (name,))
            return f"已禁用：{name}"
        return "可用操作：list / add(name,url) / remove(name) / enable(name) / disable(name)"
    except Exception as e:
        return f"操作失败：{e}"


async def handle_detect_api(event, match):
    """超管命令：直接管理检测 API 池（不依赖 AI 工具调用）。
    用法: /检测api list | add <名称> <URL> | remove <名称> | enable <名称> | disable <名称>
    """
    try:
        raw = (event.message or "").strip()
        # 去掉命令前缀（兼容 /检测api 及别名）
        for prefix in ("/检测api", "/检测API", "/api池"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        if not raw:
            raw = "list"
        parts = raw.split()
        action = parts[0].lower()
        name = parts[1] if len(parts) > 1 else ""
        url = parts[2] if len(parts) > 2 else ""
        if action in ("list", "查看", "ls"):
            rows = ctx.db_query("SELECT id, name, url, enabled FROM llm_detect_api ORDER BY id", ())
            if not rows:
                await _send(event, "📭 当前无自定义检测 API 端点（默认走配置 detect_image_urls 中的端点）")
                return
            lines = ["🔌 当前检测 API 池:"]
            for r in rows:
                st = "✅ 启用" if r["enabled"] else "⛔ 禁用"
                lines.append(f"[{r['id']}] {r['name']} {r['url']} {st}")
            await _send(event, "\n".join(lines))
            return
        if action in ("add", "添加", "新增"):
            if not name or not url:
                await _send(event, "⚠️ 用法: /检测api add <名称> <URL>")
                return
            ctx.db_execute(
                "INSERT INTO llm_detect_api (name, url, enabled, created_at) "
                "VALUES (%s, %s, 1, %s)",
                (name, url, int(time.time())))
            _log_info(f"超管添加检测API: {name} -> {url}")
            await _send(event, f"✅ 已添加检测 API：{name} → {url}\n"
                               f"AI 收到相关请求时会自动优先使用该端点。")
            return
        if action in ("remove", "del", "delete", "删除"):
            if not name:
                await _send(event, "⚠️ 用法: /检测api remove <名称>")
                return
            ctx.db_execute("DELETE FROM llm_detect_api WHERE name=%s OR id=%s",
                           (name, name if name.isdigit() else -1))
            await _send(event, f"🗑️ 已删除检测 API：{name}")
            return
        if action in ("enable", "启用"):
            ctx.db_execute("UPDATE llm_detect_api SET enabled=1 WHERE name=%s", (name,))
            await _send(event, f"✅ 已启用检测 API：{name}")
            return
        if action in ("disable", "禁用"):
            ctx.db_execute("UPDATE llm_detect_api SET enabled=0 WHERE name=%s", (name,))
            await _send(event, f"⛔ 已禁用检测 API：{name}")
            return
        await _send(event, "⚠️ 未知操作。可用: list / add <名称> <URL> / remove <名称> / enable <名称> / disable <名称>")
    except Exception as e:
        _log_err("超管检测API命令执行失败", e)
        await _send(event, f"⚠️ 操作失败：{e}")


# ================= 通用 API 池（AI 自主管理 + 自由调用） =================

# SSRF 防护：内网/保留网段默认全部拦截（配置 api_allow_ips 可白名单放行）
_BLOCKED_NETWORKS = [
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4", "240.0.0.0/4",
    "::1/128", "fc00::/7", "fe80::/10", "::/128",
]


def _api_allow_ips() -> list:
    """读取白名单配置 api_allow_ips（逗号分隔的 IP 或 CIDR）"""
    cfg = str(_get_config("api_allow_ips", "") or "").strip()
    out = []
    for item in re.split(r"[,\s，、]+", cfg):
        item = item.strip()
        if item and item not in out:
            out.append(item)
    return out


def _resolve_host_ips(host: str) -> list:
    """解析主机名为 IP 列表（含直接传 IP 的情况），失败返回空"""
    host = str(host or "").strip().rstrip(".")
    if not host:
        return []
    ips = []
    try:
        ips.append(str(ipaddress.ip_address(host)))
        return ips
    except ValueError:
        pass
    try:
        for info in socket.getaddrinfo(host, None):
            ip = info[4][0]
            if ip and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def _is_url_blocked(url: str):
    """SSRF 检查：返回 (是否拦截, 说明)。命中白名单 api_allow_ips 则放行。"""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return True, "URL 解析失败"
    if parsed.scheme not in ("http", "https"):
        return True, f"仅允许 http/https 协议，收到: {parsed.scheme}"
    host = parsed.hostname or ""
    if not host:
        return True, "URL 缺少主机名"
    ips = _resolve_host_ips(host)
    if not ips:
        return True, f"无法解析主机: {host}"
    allow_nets = []
    for a in _api_allow_ips():
        try:
            if "/" in a:
                allow_nets.append(ipaddress.ip_network(a, strict=False))
            else:
                suffix = "/128" if ":" in a else "/32"
                allow_nets.append(ipaddress.ip_network(f"{a}{suffix}", strict=False))
        except Exception:
            pass
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if any(addr in net for net in allow_nets):
            continue  # 白名单放行
        for net_str in _BLOCKED_NETWORKS:
            try:
                if addr in ipaddress.ip_network(net_str, strict=False):
                    return True, f"目标地址 {ip} 属于内网/保留网段({net_str})"
            except Exception:
                continue
    return False, ""


async def _probe_api_structure(url: str, method: str = "GET", headers_raw: str = "") -> str:
    """快速探测接口返回结构（用于写入节点注释）。超时 10s，失败返回错误描述而非抛异常。"""
    blocked, why = _is_url_blocked(url)
    if blocked:
        return f"探测被安全策略拦截（{why}）"
    import httpx
    hdrs = {"User-Agent": "ZCBOT-LLM/1.0"}
    if headers_raw:
        try:
            h = json.loads(headers_raw)
            if isinstance(h, dict):
                for k, v in h.items():
                    if v is not None:
                        hdrs[str(k)] = str(v)
        except Exception:
            pass
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=8), follow_redirects=False) as client:
            if method in ("POST", "PUT", "PATCH"):
                resp = await client.request(method, url, headers=hdrs, json={})
            else:
                resp = await client.request("GET", url, headers=hdrs)
    except Exception as e:
        return f"探测失败（{type(e).__name__}: {e}）"
    if resp.status_code not in (200, 201):
        return f"探测返回 HTTP {resp.status_code}"
    try:
        data = resp.json()
    except Exception:
        return f"返回文本(非JSON)，长度约 {len(resp.text or '')}"
    if isinstance(data, dict):
        keys = ", ".join(str(k) for k in list(data.keys())[:20])
        return f"返回JSON对象，字段: {keys}"
    if isinstance(data, list):
        sample = data[0] if data else {}
        if isinstance(sample, dict):
            keys = ", ".join(str(k) for k in list(sample.keys())[:20])
            return f"返回JSON数组(共{len(data)}项)，元素字段: {keys}"
        return f"返回JSON数组(共{len(data)}项)，元素类型: {type(sample).__name__}"
    return f"返回JSON类型: {type(data).__name__}"


async def _fn_manage_api_pool(args, ctx=None, event=None, user_id=None):
    """AI 函数：增删查启停/写注释通用 API 池节点"""
    action = str(args.get("action") or "list").strip().lower()
    name = str(args.get("name") or "").strip()
    url = str(args.get("url") or "").strip()
    method = str(args.get("method") or "GET").strip().upper() or "GET"
    headers_raw = str(args.get("headers") or "").strip()
    note = str(args.get("note") or "").strip()
    try:
        if action in ("list", "查看"):
            rows = ctx.db_query("SELECT id, name, url, method, enabled, note FROM llm_api_pool ORDER BY id", ())
            if not rows:
                return "当前通用 API 池为空。可用 manage_api_pool 的 add(name,url,note) 添加节点。"
            lines = []
            for r in rows:
                base = f"[{r['id']}] {r['name']} {r['method']} {r['url']} enabled={r['enabled']}"
                n = str(r.get("note") or "").strip()
                if n:
                    base += f"\n    注: {n[:200]}"
                lines.append(base)
            return "\n".join(lines)
        if action in ("add", "新增", "添加"):
            if not name or not url:
                return "错误：add 需要 name 和 url"
            if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                method = "GET"
            headers_store = ""
            if headers_raw:
                try:
                    json.loads(headers_raw)
                    headers_store = headers_raw
                except Exception:
                    return "错误：headers 必须是合法 JSON 对象字符串，如 {\"Authorization\":\"Bearer xxx\"}"
            # 硬性要求：添加时自动探测返回结构并写入注释
            probe = ""
            try:
                probe = await _probe_api_structure(url, method, headers_store)
            except Exception as e:
                probe = f"探测失败（{type(e).__name__}: {e}）"
            note_parts = [p for p in (note, probe) if p and p.strip()]
            note_store = " | ".join(note_parts)[:1000]
            ctx.db_execute(
                "INSERT INTO llm_api_pool (name, url, method, headers, note, enabled, created_at) "
                "VALUES (%s, %s, %s, %s, %s, 1, %s)",
                (name, url, method, headers_store, note_store, int(time.time())))
            return (f"已添加 API 节点：{name} → {method} {url}"
                    + (f"\n📝 返回结构探测：{probe}" if probe else ""))
        if action in ("comment", "note", "注释"):
            if not name:
                return "错误：comment 需要 name（节点名称或 id）"
            ctx.db_execute("UPDATE llm_api_pool SET note=%s WHERE name=%s OR id=%s",
                           (note, name, name if name.isdigit() else -1))
            return f"已更新节点「{name}」注释：{note}" if note else f"已清空节点「{name}」注释"
        if action in ("remove", "del", "delete", "删除"):
            if not name:
                return "错误：remove 需要 name（或 id）"
            ctx.db_execute("DELETE FROM llm_api_pool WHERE name=%s OR id=%s",
                           (name, name if name.isdigit() else -1))
            return f"已删除 API 节点：{name}"
        if action in ("enable", "启用"):
            ctx.db_execute("UPDATE llm_api_pool SET enabled=1 WHERE name=%s", (name,))
            return f"已启用：{name}"
        if action in ("disable", "禁用"):
            ctx.db_execute("UPDATE llm_api_pool SET enabled=0 WHERE name=%s", (name,))
            return f"已禁用：{name}"
        return "可用操作：list / add(name,url,method,headers,note) / comment(name,note) / remove(name) / enable(name) / disable(name)"
    except Exception as e:
        return f"操作失败：{e}"


async def _fn_call_api(args, ctx=None, event=None, user_id=None):
    """AI 函数：调用通用 API 池节点或任意 HTTP(S) 接口，返回 JSON/文本"""
    name = str(args.get("name") or "").strip()
    url = str(args.get("url") or "").strip()
    method = str(args.get("method") or "GET").strip().upper() or "GET"
    params = args.get("params") or {}
    json_body = args.get("json_body")
    headers_extra = args.get("headers") or {}
    try:
        timeout = max(1, min(int(args.get("timeout") or 15), 60))
    except (TypeError, ValueError):
        timeout = 15

    # 1) 按节点名取池内配置
    if name:
        try:
            row = ctx.db_query_one(
                "SELECT name, url, method, headers FROM llm_api_pool WHERE name=%s AND enabled=1", (name,))
        except Exception as e:
            return f"错误：查询 API 池失败: {e}"
        if not row:
            return (f"错误：API 池中不存在启用的节点「{name}」。"
                    f"可先 manage_api_pool list 查看现有节点，或用 add(name,url) 添加。")
        url = str(row.get("url") or "").strip()
        method = method or str(row.get("method") or "GET").strip().upper() or "GET"
        try:
            node_headers = json.loads(row.get("headers") or "{}")
            if isinstance(node_headers, dict):
                for k, v in node_headers.items():
                    headers_extra.setdefault(k, v)
        except Exception:
            pass
    if not url:
        return "错误：需要 name（API 池节点）或 url（直接请求地址）"

    # 2) SSRF 防护（默认拦截内网/保留地址，白名单放行）
    blocked, why = _is_url_blocked(url)
    if blocked:
        _log_warn(f"call_api 被 SSRF 拦截: {url} 原因={why} user={user_id}")
        return (f"错误：该地址被安全策略拦截（{why}）。仅可访问公网地址；"
                f"如需访问内网，请把目标 IP 加入配置 api_allow_ips 白名单。")

    # 3) 附加查询参数
    if params and isinstance(params, dict):
        qs = urllib.parse.urlencode({str(k): str(v) for k, v in params.items() if v is not None})
        if qs:
            url = f"{url}{'&' if '?' in url else '?'}{qs}"

    # 4) 发起请求（不跟随重定向，防 SSRF 绕过）
    import httpx
    headers = {"User-Agent": "ZCBOT-LLM/1.0"}
    if isinstance(headers_extra, dict):
        for k, v in headers_extra.items():
            if v is not None:
                headers[str(k)] = str(v)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=timeout),
                                     follow_redirects=False) as client:
            if method in ("POST", "PUT", "PATCH"):
                resp = await client.request(method, url, headers=headers,
                                            json=json_body if json_body is not None else {})
            else:
                resp = await client.request("GET", url, headers=headers)
    except Exception as e:
        _log_warn(f"call_api 请求失败: {url} {type(e).__name__}: {e}")
        return f"错误：请求失败（{type(e).__name__}: {e}）"
    if resp.status_code not in (200, 201):
        return f"错误：HTTP {resp.status_code}，响应: {str(resp.text or '')[:300]}"

    # 5) 解析响应：JSON 优先，非 JSON 给原文（原样交还 AI 自由分析）
    try:
        data = resp.json()
        out = json.dumps(data, ensure_ascii=False)
    except Exception:
        out = resp.text or ""
    # 可选：返回内容走违禁词检测（配置 api_detect_response=True 开启）
    if str(_get_config("api_detect_response", False)).lower() in ("true", "1", "yes", "on"):
        try:
            r = await _check_content(out)
            if not r["ok"]:
                return f"警告：返回内容未通过安全检测（{r['reason']}），已截断展示：\n{_clip(out, 500)}"
        except Exception:
            pass
    max_len = int(_get_config("api_max_response", 6000) or 6000)
    out = _clip(out, max_len)
    _log_info(f"call_api 成功: {url} status={resp.status_code} len={len(out)} user={user_id}")
    return f"调用成功（HTTP {resp.status_code}）。返回数据：\n{out}"


async def handle_api_pool(event, match):
    """超管命令：直接管理通用 API 池（不依赖 AI 工具调用）。
    用法: /api池 list | add <名称> <URL> [GET|POST] | note <名称> <注释> | remove <名称> | enable <名称> | disable <名称>
    """
    try:
        raw = (event.message or "").strip()
        for prefix in ("/api池", "/apipool", "/api_pool"):
            if raw.lower().startswith(prefix.lower()):
                raw = raw[len(prefix):].strip()
                break
        if not raw:
            raw = "list"
        parts = raw.split()
        action = parts[0].lower()
        name = parts[1] if len(parts) > 1 else ""
        url = parts[2] if len(parts) > 2 else ""
        method = parts[3].upper() if len(parts) > 3 else "GET"
        if action in ("list", "查看", "ls"):
            rows = ctx.db_query("SELECT id, name, url, method, enabled, note FROM llm_api_pool ORDER BY id", ())
            if not rows:
                await _send(event, "📭 当前通用 API 池为空。可用 /api池 add <名称> <URL> 添加节点。")
                return
            lines = ["🔌 当前通用 API 池:"]
            for r in rows:
                st = "✅ 启用" if r["enabled"] else "⛔ 禁用"
                line = f"[{r['id']}] {r['name']} {r['method']} {r['url']} {st}"
                n = str(r.get("note") or "").strip()
                if n:
                    line += f"\n    📝 {n[:200]}"
                lines.append(line)
            await _send(event, "\n".join(lines))
            return
        if action in ("add", "添加", "新增"):
            if not name or not url:
                await _send(event, "⚠️ 用法: /api池 add <名称> <URL> [GET|POST]")
                return
            if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                method = "GET"
            # 添加时自动探测返回结构并写入注释
            probe = ""
            try:
                probe = await _probe_api_structure(url, method, "")
            except Exception as e:
                probe = f"探测失败（{type(e).__name__}: {e}）"
            ctx.db_execute(
                "INSERT INTO llm_api_pool (name, url, method, headers, note, enabled, created_at) "
                "VALUES (%s, %s, %s, '', %s, 1, %s)",
                (name, url, method, probe, int(time.time())))
            _log_info(f"超管添加通用API节点: {name} -> {method} {url}")
            await _send(event, f"✅ 已添加 API 节点：{name} → {method} {url}\n"
                               f"📝 返回结构探测：{probe}\n"
                               f"AI 可用 call_api 调用该节点获取数据。")
            return
        if action in ("note", "注释"):
            if not name:
                await _send(event, "⚠️ 用法: /api池 note <名称> <注释>")
                return
            rest = raw.split(maxsplit=2)
            note_text = rest[2].strip() if len(rest) > 2 else ""
            ctx.db_execute("UPDATE llm_api_pool SET note=%s WHERE name=%s OR id=%s",
                           (note_text, name, name if name.isdigit() else -1))
            await _send(event, f"📝 已更新节点「{name}」注释：{note_text}")
            return
        if action in ("remove", "del", "delete", "删除"):
            if not name:
                await _send(event, "⚠️ 用法: /api池 remove <名称>")
                return
            ctx.db_execute("DELETE FROM llm_api_pool WHERE name=%s OR id=%s",
                           (name, name if name.isdigit() else -1))
            await _send(event, f"🗑️ 已删除 API 节点：{name}")
            return
        if action in ("enable", "启用"):
            ctx.db_execute("UPDATE llm_api_pool SET enabled=1 WHERE name=%s", (name,))
            await _send(event, f"✅ 已启用 API 节点：{name}")
            return
        if action in ("disable", "禁用"):
            ctx.db_execute("UPDATE llm_api_pool SET enabled=0 WHERE name=%s", (name,))
            await _send(event, f"⛔ 已禁用 API 节点：{name}")
            return
        await _send(event, "⚠️ 未知操作。可用: list / add <名称> <URL> [GET|POST] / note <名称> <注释> / remove <名称> / enable <名称> / disable <名称>")
    except Exception as e:
        _log_err("超管通用API池命令执行失败", e)
        await _send(event, f"⚠️ 操作失败：{e}")


async def _save_history(user_id: str, group_id, role: str, content: str):
    """把一条对话记录写入 llm_chat_history（写入前先安全检测，违规不入库），并清理该用户超出上限的最旧记录。"""
    if not content:
        return
    # 写入前检测：违禁词 + 图片（detect_on_write 开关控制）
    if _detect_on_write():
        try:
            r = await _check_content(content)
            if not r["ok"]:
                _log_warn(f"内容未通过写入检测，已拦截入库 role={role} reason={r['reason']}")
                return
        except Exception as e:
            _log_warn(f"写入前检测异常(放行): {e}")
    try:
        ctx.db_execute(
            "INSERT INTO llm_chat_history (user_id, group_id, role, content, ts) "
            "VALUES (%s, %s, %s, %s, %s)",
            (str(user_id), str(group_id or ""), str(role), str(content)[:4000],
             int(time.time())),
        )
        try:
            ctx.db_execute(
                "DELETE FROM llm_chat_history WHERE user_id=%s AND id NOT IN ("
                "SELECT id FROM ("
                "SELECT id FROM llm_chat_history WHERE user_id=%s "
                "ORDER BY id DESC LIMIT %s"
                ") t)",
                (str(user_id), str(user_id), _HISTORY_MAX_PER_USER),
            )
        except Exception as e:
            _log_warn(f"历史清理失败(忽略): {e}")
    except Exception as e:
        _log_warn(f"历史写入失败: {e}")


def _get_history(user_id: str) -> list:
    now = time.time()
    expired = [uid for uid, s in _SESSIONS.items() if now - s["ts"] > _SESSION_TTL]
    for uid in expired:
        _SESSIONS.pop(uid, None)
    sess = _SESSIONS.get(user_id)
    if not sess or now - sess["ts"] > _SESSION_TTL:
        sess = {"msgs": [], "ts": now}
        _SESSIONS[user_id] = sess
    sess["ts"] = now
    return sess["msgs"]


def _is_blacklisted(user_id: str) -> bool:
    try:
        row = ctx.db_query_one(
            "SELECT 1 FROM llm_blacklist WHERE user_id=%s", (user_id,))
        return bool(row)
    except Exception:
        return False  # 表不存在/异常时保守放行


# ================= 好感度 =================

def _affinity_level(v: int) -> str:
    """好感度等级划分"""
    if v < 0:
        return "厌恶"
    if v < 20:
        return "陌生"
    if v < 50:
        return "初识"
    if v < 100:
        return "熟悉"
    if v < 200:
        return "亲密"
    return "挚友"


def _get_affinity_value(user_id: str):
    """读取好感度，无记录返回 None"""
    try:
        row = ctx.db_query_one(
            "SELECT affinity FROM llm_affinity WHERE user_id=%s", (str(user_id),))
        return int(row["affinity"]) if row else None
    except Exception as e:
        _log_warn(f"好感度读取失败: {e}")
        return None


def _add_affinity(user_id: str, delta: int):
    """增加好感度（可负），返回新值"""
    uid = str(user_id)
    try:
        ctx.db_execute(
            "INSERT INTO llm_affinity (user_id, affinity, updated_at) "
            "VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE affinity=affinity+%s, updated_at=%s",
            (uid, max(0, delta), int(time.time()), delta, int(time.time())),
        )
        v = _get_affinity_value(uid)
        _log_info(f"好感度变更 user={uid} delta={delta} now={v}")
        return v
    except Exception as e:
        _log_warn(f"好感度写入失败: {e}")
        return None


async def _fn_get_affinity(args, ctx=None, event=None, user_id=None):
    """AI 函数：读取好感度"""
    uid = str(args.get("user_id") or user_id or "").strip()
    if not uid:
        return "错误：缺少 user_id 参数"
    v = _get_affinity_value(uid)
    if v is None:
        return f"用户 {uid} 暂无好感度记录（从第一次成功对话开始累计）。"
    return f"用户 {uid} 的好感度: {v}（等级: {_affinity_level(v)}）"


async def _fn_change_affinity(args, ctx=None, event=None, user_id=None):
    """AI 函数：调整好感度（±5 内）"""
    uid = str(args.get("user_id") or "").strip()
    try:
        delta = int(args.get("delta") or 0)
    except (TypeError, ValueError):
        return "错误：delta 必须是整数"
    delta = max(-5, min(5, delta))  # 限幅 ±5
    reason = str(args.get("reason") or "")[:100]
    if not uid:
        return "错误：缺少 user_id 参数"
    if delta == 0:
        return "好感度未变化（delta=0）"
    new_v = _add_affinity(uid, delta)
    _log_info(f"AI调整好感度 user={uid} delta={delta} reason={reason} new={new_v}")
    return (f"已{'增加' if delta > 0 else '减少'}用户 {uid} 的好感度 {abs(delta)} 点"
            f"（当前: {new_v}，等级: {_affinity_level(new_v or 0)}）"
            + (f"，原因: {reason}" if reason else ""))


# ================= 人格预设系统（人格预设系统） =================

def _persona_prompt() -> str:
    """读取当前启用人格的 prompt，未启用/未设置返回空串"""
    if not _cfg_bool("enable_persona", True):
        return ""
    cur = str(_get_config("current_persona", "") or "").strip()
    if not cur:
        return ""
    try:
        row = ctx.db_query_one(
            "SELECT prompt FROM llm_personas WHERE name=%s AND enabled=1", (cur,))
        if row and row.get("prompt"):
            return str(row["prompt"]).strip()
    except Exception as e:
        _log_warn(f"人格读取失败: {e}")
    return ""


async def handle_persona(event, match):
    """/人格 指令：list / use <名称> / add <名称> <prompt> / del <名称> / off"""
    text = (event.message or "").strip()
    for prefix in ("/人格", "/persona", "/人设"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    args = text.split(maxsplit=1)
    op = (args[0] if args else "list").lower()
    rest = args[1].strip() if len(args) > 1 else ""
    is_super = getattr(event, "role", "") == "super" or getattr(event, "is_superuser", False)

    try:
        if op in ("list", "ls", ""):
            rows = ctx.db_query(
                "SELECT id, name, prompt, enabled FROM llm_personas ORDER BY id", ()) or []
            if not rows:
                await _send(event, "🎭 暂无任何人格预设。\n用法: /人格 add <名称> <人格设定描述>\n"
                                   "示例: /人格 add 猫娘 你是一只傲娇的猫娘，喜欢用喵结尾说话")
                return
            cur = str(_get_config("current_persona", "") or "")
            lines = ["🎭 人格列表（" + ("当前: " + cur if cur else "当前: 默认人格") + "）:"]
            for r in rows:
                mark = " ✅" if r["name"] == cur else ""
                p = str(r.get("prompt") or "")[:60].replace("\n", " ")
                lines.append(f"• {r['name']}{mark} — {p}")
            lines.append("\n用法: /人格 use <名称> | off | add <名称> <描述> | del <名称>")
            await _send(event, "\n".join(lines))
            return

        if op in ("use", "启用", "切换"):
            if not rest:
                await _send(event, "用法: /人格 use <名称>")
                return
            row = ctx.db_query_one(
                "SELECT name FROM llm_personas WHERE name=%s AND enabled=1", (rest,))
            if not row:
                await _send(event, f"❌ 人格「{rest}」不存在或已停用")
                return
            _set_config_value("current_persona", rest)
            await _send(event, f"🎭 已切换到人格「{rest}」，下次对话生效。")
            _schedule_snapshot()
            return

        if op in ("off", "关闭", "默认"):
            _set_config_value("current_persona", "")
            await _send(event, "🎭 已切回默认人格。")
            _schedule_snapshot()
            return

        if op in ("add", "新增", "新建"):
            if not is_super:
                await _send(event, "🔒 仅超级管理员可新增人格")
                return
            if not rest:
                await _send(event, "用法: /人格 add <名称> <人格设定描述>")
                return
            name, _, prompt = rest.partition(" ")
            name = name.strip()
            prompt = prompt.strip()
            if not name or not prompt:
                await _send(event, "用法: /人格 add <名称> <人格设定描述>")
                return
            existed = ctx.db_query_one("SELECT id FROM llm_personas WHERE name=%s", (name,))
            if existed:
                ctx.db_execute("UPDATE llm_personas SET prompt=%s, enabled=1 WHERE name=%s",
                               (prompt, name))
                await _send(event, f"🎭 人格「{name}」已更新。")
            else:
                ctx.db_insert(
                    "INSERT INTO llm_personas (name, prompt, enabled, created_at) "
                    "VALUES (%s, %s, 1, %s)",
                    (name, prompt, int(time.time())))
                await _send(event, f"🎭 人格「{name}」已添加。可用 /人格 use {name} 启用")
            _schedule_snapshot()
            return

        if op in ("del", "删除", "remove"):
            if not is_super:
                await _send(event, "🔒 仅超级管理员可删除人格")
                return
            if not rest:
                await _send(event, "用法: /人格 del <名称>")
                return
            ctx.db_execute("DELETE FROM llm_personas WHERE name=%s", (rest,))
            cur = str(_get_config("current_persona", "") or "")
            if cur == rest:
                _set_config_value("current_persona", "")
                await _send(event, f"🗑 已删除人格「{rest}」，并切回默认人格。")
            else:
                await _send(event, f"🗑 已删除人格「{rest}」。")
            _schedule_snapshot()
            return

        await _send(event, "❓ 未知操作。用法: /人格 list | use <名称> | off | add <名称> <描述> | del <名称>")
    except Exception as e:
        _log_err("人格指令异常", e)
        await _send(event, f"❌ 人格操作失败: {e}")


async def handle_affinity(event, match):
    """查看自己的好感度"""
    user_id = str(event.user_id)
    v = _get_affinity_value(user_id)
    if v is None:
        await _send(event, "💖 你还没有好感度记录，和我聊聊天就能开始累计啦~")
        return
    await _send(event, f"💖 你的好感度: {v}（等级: {_affinity_level(v)}）\n"
                       f"每成功对话一次 +{int(_get_config('affinity_step', 1) or 1)}，"
                       f"多聊天、多互动可以提升哦~")


# ================= 长期记忆 =================

def _important_memories_block(user_id: str) -> str:
    """取该用户重要记忆（最多配置条数），拼成注入上下文的文本块"""
    try:
        max_n = max(1, min(int(_get_config("important_memories_max", 5) or 5), 10))
    except Exception:
        max_n = 5
    try:
        rows = ctx.db_query(
            "SELECT content, updated_at FROM llm_memory "
            "WHERE user_id=%s AND important=1 ORDER BY id DESC LIMIT %s",
            (str(user_id), max_n))
    except Exception as e:
        _log_warn(f"重要记忆读取失败: {e}")
        return ""
    if not rows:
        return ""
    lines = [f"【{user_id} 的重要长期记忆（{len(rows)}条，请始终牢记并融入回答）】"]
    for r in reversed(rows):
        t = time.strftime("%m-%d", time.localtime(int(r.get("updated_at") or 0)))
        lines.append(f"- [{t}] {str(r.get('content') or '')[:200]}")
    return "\n".join(lines)


async def _fn_memory_write(args, ctx=None, event=None, user_id=None):
    """AI 函数：写入长期记忆"""
    uid = str(args.get("user_id") or user_id or "").strip()
    content = str(args.get("content") or "").strip()
    try:
        important = 1 if int(args.get("important") or 0) in (1, "1", "true") else 0
    except Exception:
        important = 0
    if not content:
        return "错误：缺少 content 参数"
    if not uid:
        return "错误：缺少 user_id 参数"
    content = content[:500]
    try:
        mid = ctx.db_insert(
            "INSERT INTO llm_memory (user_id, content, important, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (uid, content, important, int(time.time()), int(time.time())),
        )
        # 每用户上限清理
        try:
            limit = max(10, int(_get_config("memory_limit_per_user", 200) or 200))
            ctx.db_execute(
                "DELETE FROM llm_memory WHERE user_id=%s AND id NOT IN ("
                "SELECT id FROM (SELECT id FROM llm_memory WHERE user_id=%s "
                "ORDER BY id DESC LIMIT %s) t)",
                (uid, uid, limit),
            )
        except Exception as e:
            _log_warn(f"记忆清理失败(忽略): {e}")
        _log_info(f"记忆写入 user={uid} id={mid} important={important} content={content[:60]}")
        return (f"已保存长期记忆 #{mid}（{'⭐重要' if important else '普通'}）：{content[:80]}"
                + ("，该条会注入后续对话上下文" if important else ""))
    except Exception as e:
        _log_err(f"记忆写入失败 user={uid}", e)
        return f"错误：保存记忆失败: {e}"


async def _fn_memory_read(args, ctx=None, event=None, user_id=None):
    """AI 函数：读取长期记忆（可按关键词过滤）"""
    uid = str(args.get("user_id") or user_id or "").strip()
    keyword = str(args.get("keyword") or "").strip()
    try:
        limit = max(1, min(int(args.get("limit") or 20), 50))
    except (TypeError, ValueError):
        limit = 20
    if not uid:
        return "错误：缺少 user_id 参数"
    try:
        if keyword:
            rows = ctx.db_query(
                "SELECT id, content, important, updated_at FROM llm_memory "
                "WHERE user_id=%s AND content LIKE %s "
                "ORDER BY important DESC, id DESC LIMIT %s",
                (uid, f"%{keyword}%", limit))
        else:
            rows = ctx.db_query(
                "SELECT id, content, important, updated_at FROM llm_memory "
                "WHERE user_id=%s ORDER BY important DESC, id DESC LIMIT %s",
                (uid, limit))
    except Exception as e:
        _log_err(f"记忆读取失败 user={uid}", e)
        return f"错误：读取记忆失败: {e}"
    if not rows:
        return f"用户 {uid} 没有" + (f"包含「{keyword}」的" if keyword else "") + "长期记忆记录。"
    lines = [f"用户 {uid} 的长期记忆（{len(rows)}条）:"]
    for r in rows:
        mark = "⭐重要" if int(r.get("important") or 0) else "普通"
        t = time.strftime("%m-%d", time.localtime(int(r.get("updated_at") or 0)))
        lines.append(f"[#{r['id']}][{mark}][{t}] {str(r.get('content') or '')[:200]}")
    return "\n".join(lines)


async def _fn_memory_delete(args, ctx=None, event=None, user_id=None):
    """AI 函数：删除长期记忆"""
    uid = str(args.get("user_id") or user_id or "").strip()
    try:
        mid = int(args.get("id") or 0)
    except (TypeError, ValueError):
        return "错误：id 必须是整数"
    if not uid or not mid:
        return "错误：缺少 user_id 或 id 参数"
    try:
        n = ctx.db_execute(
            "DELETE FROM llm_memory WHERE id=%s AND user_id=%s", (mid, uid))
        if n:
            _log_info(f"记忆删除 user={uid} id={mid}")
            return f"已删除用户 {uid} 的长期记忆 #{mid}"
        return f"未找到记忆 #{mid}（可能已删除或不属于该用户）"
    except Exception as e:
        _log_err(f"记忆删除失败 user={uid} id={mid}", e)
        return f"错误：删除记忆失败: {e}"


async def handle_memory(event, match):
    """查看自己的长期记忆列表"""
    user_id = str(event.user_id)
    try:
        rows = ctx.db_query(
            "SELECT id, content, important, updated_at FROM llm_memory "
            "WHERE user_id=%s ORDER BY important DESC, id DESC LIMIT 50", (user_id,))
    except Exception as e:
        _log_err(f"/记忆 查询失败 user={user_id}", e)
        await _send(event, "⚠️ 查询记忆失败，请稍后再试。")
        return
    if not rows:
        await _send(event, "🧠 你还没有长期记忆。AI 会在聊天中把重要的事情记下来~")
        return
    lines = [f"🧠 你的长期记忆（共 {len(rows)} 条，⭐=重要会注入上下文）:"]
    for r in rows:
        mark = "⭐" if int(r.get("important") or 0) else "·"
        t = time.strftime("%m-%d %H:%M", time.localtime(int(r.get("updated_at") or 0)))
        lines.append(f"{mark} [#{r['id']}] [{t}] {str(r.get('content') or '')[:100]}")
    await _send(event, "\n".join(lines))


# ================= 统计 / 会话状态 / 函数列表（统计 / 会话状态 / 函数列表） =================

def _dashboard_card_data():
    """仪表盘卡片数据（WebUI 首页展示）"""
    try:
        today = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")))
        row = ctx.db_query_one(
            "SELECT COUNT(*) AS c FROM llm_chat_history WHERE role='user' AND ts>=%s", (today,))
        today_chats = int(row["c"]) if row else 0
    except Exception:
        today_chats = 0
    with _STATS_LOCK:
        stats = dict(_STATS)
    func_n = 0
    try:
        with _LLM_FUNCTIONS_LOCK:
            func_n = len(_LLM_FUNCTIONS)
    except Exception:
        pass
    return {
        "title": "LLM 对话",
        "value": f"{stats.get('chats', 0)} 次",
        "label": f"今日 {today_chats} · 工具调用 {stats.get('tool_calls', 0)} · 函数 {func_n} 个",
        "icon": "🤖",
        "color": "#34c759",
    }


async def handle_stats(event, match):
    """查看 LLM 对话统计"""
    try:
        today = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")))
        row = ctx.db_query_one(
            "SELECT COUNT(*) AS c FROM llm_chat_history WHERE role='user' AND ts>=%s", (today,))
        today_chats = int(row["c"]) if row else 0
        row2 = ctx.db_query_one("SELECT COUNT(*) AS c FROM llm_chat_history WHERE role='user'", ())
        total_chats = int(row2["c"]) if row2 else 0
    except Exception as e:
        _log_warn(f"统计查询失败: {e}")
        today_chats = total_chats = 0
    with _STATS_LOCK:
        stats = dict(_STATS)
    with _LLM_FUNCTIONS_LOCK:
        func_n = len(_LLM_FUNCTIONS)
    cur = str(_get_config("current_persona", "") or "")
    lines = [
        "📊 LLM 对话统计",
        f"• 内存会话数: {len(_SESSIONS)}",
        f"• 本进程对话次数: {stats.get('chats', 0)}",
        f"• 今日对话(入库): {today_chats}",
        f"• 历史对话总数(入库): {total_chats}",
        f"• 工具调用次数: {stats.get('tool_calls', 0)}（失败 {stats.get('tool_errors', 0)}）",
        f"• 可调用函数: {func_n} 个（/函数列表 查看）",
        f"• 当前人格: {cur or '默认人格'}",
    ]
    await _send(event, "\n".join(lines))


async def handle_session(event, match):
    """查看当前会话上下文状态"""
    user_id = str(event.user_id)
    sess = _SESSIONS.get(user_id)
    if not sess or not sess.get("msgs"):
        await _send(event, "💬 你当前没有进行中的会话。发 /chat <内容> 开始对话吧~")
        return
    msgs = sess["msgs"]
    roles = {}
    for m in msgs:
        r = str(m.get("role") or "?")
        roles[r] = roles.get(r, 0) + 1
    total_chars = sum(len(str(m.get("content") or "")) for m in msgs)
    tools = sum(1 for m in msgs if m.get("tool_calls"))
    parts = "、".join(f"{k}×{v}" for k, v in roles.items())
    await _send(event,
                f"💬 当前会话状态\n"
                f"• 消息总数: {len(msgs)} 条（{parts}）\n"
                f"• 总字符: {total_chars}\n"
                f"• 工具调用消息: {tools} 条\n"
                f"• 最后活跃: {time.strftime('%H:%M:%S', time.localtime(sess.get('ts', 0)))}"
                f"（/chat clear 可清空）")


async def handle_fn_list(event, match):
    """查看 LLM 当前可调用的函数列表"""
    with _LLM_FUNCTIONS_LOCK:
        funcs = sorted(
            ({"name": n, "description": str(i.get("description") or ""),
              "plugin": str(i.get("plugin_name") or "")}
             for n, i in _LLM_FUNCTIONS.items()),
            key=lambda x: x["name"])
    if not funcs:
        await _send(event, "🛠 当前没有可调用的函数。")
        return
    lines = [f"🛠 LLM 可调用函数（共 {len(funcs)} 个）:"]
    for f in funcs:
        lines.append(f"• {f['name']}〔{f['plugin'] or 'llm_chat'}〕\n  {str(f['description'])[:120]}")
    await _send(event, "\n".join(lines))


# ================= OneBot API（异步 + 缓存，避免同步桥接超时） =================

async def _aapi(action, ttl=None, **params):
    """异步调用 OneBot API，带缓存。返回 data 字段（dict/list）或 None。

    修复：旧版用 ctx.api()（同步桥接 run_coroutine_threadsafe），
    在 async 处理器里调用会自我阻塞 → 15s 超时。这里全部改用 ctx.aapi() 异步。
    """
    key = (action, json.dumps(params, sort_keys=True, ensure_ascii=False, default=str))
    now = time.time()
    if ttl:
        with _API_CACHE_LOCK:
            hit = _API_CACHE.get(key)
        if hit and now - hit[0] < ttl:
            _log_info(f"API缓存命中 {action} {params}")
            return hit[1]
    try:
        resp = await ctx.aapi(action, **params)
    except Exception as e:
        _log_warn(f"OneBot API 调用失败 {action} {params}: {e}")
        return None
    data = resp.get("data") if isinstance(resp, dict) else resp
    if ttl:
        with _API_CACHE_LOCK:
            if len(_API_CACHE) > _API_CACHE_MAX:
                _API_CACHE.clear()  # 防缓存无限增长
            _API_CACHE[key] = (now, data)
        _log_info(f"API调用 {action} {params} → {'成功' if data is not None else '无数据'}")
    return data


def _safe_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ================= AI 可调用函数（内置群能力） =================

async def _fn_get_chat_history(args, ctx=None, event=None, user_id=None):
    """AI 函数：追溯某用户的 LLM 对话历史"""
    uid = str(args.get("user_id") or user_id or "").strip()
    try:
        limit = max(1, min(int(args.get("limit") or 20), 200))
    except (TypeError, ValueError):
        limit = 20
    if not uid:
        return "错误：缺少 user_id 参数"
    try:
        rows = ctx.db_query(
            "SELECT role, content, ts FROM llm_chat_history "
            "WHERE user_id=%s ORDER BY id DESC LIMIT %s", (uid, limit))
    except Exception as e:
        _log_err(f"追溯对话历史失败 user={uid}", e)
        return f"错误：查询对话历史失败: {e}"
    if not rows:
        return f"用户 {uid} 暂无 LLM 对话历史记录。"
    role_map = {"user": "用户", "assistant": "AI", "system": "系统"}
    lines = [f"用户 {uid} 的最近对话历史（共 {len(rows)} 条）:"]
    for r in reversed(rows):
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(r["ts"] or 0)))
        role = role_map.get(str(r.get("role") or ""), str(r.get("role") or "?"))
        lines.append(f"[{t}] {role}: {str(r.get('content') or '')[:300]}")
    return "\n".join(lines)


async def _fn_get_group_msgs(args, ctx=None, event=None, user_id=None):
    """AI 函数：获取群最近消息（NapCat get_group_msg_history，带群号）"""
    gid = str(args.get("group_id") or
               (event.group_id if event and getattr(event, "is_group", False) else "") or "")
    try:
        limit = max(1, min(int(args.get("limit") or 20), 100))
    except (TypeError, ValueError):
        limit = 20
    if not gid:
        return "错误：缺少 group_id 参数"
    data = await _aapi("get_group_msg_history", group_id=_safe_int(gid), count=limit)
    msgs = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(msgs, list):
        return f"错误：无法获取群 {gid} 消息（接口可能不支持或权限不足）"
    lines = [f"[群 {gid} 最近 {len(msgs)} 条消息]"]
    for m in msgs:
        if not isinstance(m, dict):
            continue
        sender = m.get("sender") or {}
        uid = m.get("user_id") or sender.get("user_id") or "?"
        nickname = sender.get("card") or sender.get("nickname") or str(uid)
        text = m.get("message")
        if isinstance(text, list):  # CQ 消息数组转纯文本
            parts = []
            for s in text:
                if not isinstance(s, dict):
                    parts.append(str(s))
                elif s.get("type") == "text":
                    parts.append(str(s.get("data", {}).get("text", "")))
                else:
                    parts.append(f"[{s.get('type')}]")
            text = "".join(parts)
        t = time.strftime("%H:%M", time.localtime(int(m.get("time") or time.time())))
        lines.append(f"{t} {nickname}({uid}): {str(text)[:200]}")
    return "\n".join(lines)


async def _fn_get_private_msgs(args, ctx=None, event=None, user_id=None):
    """AI 函数：获取与指定好友的私聊消息（NapCat get_friend_msg_history 实时拉取，不落库）"""
    uid = str(args.get("user_id") or "").strip()
    if not uid:
        return "错误：缺少 user_id 参数（要查询与哪个 QQ 的私聊记录）"
    try:
        limit = max(1, min(int(args.get("limit") or 20), 200))
    except (TypeError, ValueError):
        limit = 20
    data = await _aapi("get_friend_msg_history", user_id=_safe_int(uid), count=limit)
    msgs = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(msgs, list):
        return f"错误：无法获取与 {uid} 的私聊记录（接口可能不支持、非好友或无权限）"
    lines = [f"[与 {uid} 的最近私聊 {len(msgs)} 条]"]
    for m in msgs:
        if not isinstance(m, dict):
            continue
        sender = m.get("sender") or {}
        suid = str(m.get("user_id") or sender.get("user_id") or "")
        direction = "对方" if suid == uid else "你"
        nickname = (sender.get("card") or sender.get("nickname") or
                    (uid if suid == uid else "我"))
        text = m.get("message")
        if isinstance(text, list):  # CQ 消息数组转纯文本
            parts = []
            for s in text:
                if not isinstance(s, dict):
                    parts.append(str(s))
                elif s.get("type") == "text":
                    parts.append(str(s.get("data", {}).get("text", "")))
                elif s.get("type") == "image":
                    parts.append("[图片]")
                elif s.get("type") == "face":
                    parts.append("[表情]")
                elif s.get("type") == "at":
                    parts.append(f"@{s.get('data', {}).get('qq', '')}")
                else:
                    parts.append(f"[{s.get('type')}]")
            text = "".join(parts)
        t = m.get("time") or 0
        tstr = time.strftime("%m-%d %H:%M", time.localtime(int(t))) if t else "?"
        lines.append(f"{tstr} {direction} {nickname}: {str(text or '')[:200]}")
    return "\n".join(lines)


async def _fn_get_group_members(args, ctx=None, event=None, user_id=None):
    """AI 函数：获取群成员列表（一次最多返回 200 条，节省 token）"""
    gid = str(args.get("group_id") or
               (event.group_id if event and getattr(event, "is_group", False) else "") or "")
    if not gid:
        return "错误：缺少 group_id 参数"
    data = await _aapi("get_group_member_list", ttl=_API_CACHE_TTL, group_id=_safe_int(gid))
    if not isinstance(data, list):
        return f"错误：无法获取群 {gid} 成员列表"
    members = data[:200]
    role_map = {"owner": "群主", "admin": "管理员", "member": "成员"}
    lines = [f"[群 {gid} 成员列表，共 {len(data)} 人，展示前 {len(members)} 条]"]
    for m in members:
        if not isinstance(m, dict):
            continue
        uid = m.get("user_id")
        nickname = m.get("card") or m.get("nickname") or str(uid)
        role = role_map.get(str(m.get("role") or ""), str(m.get("role") or "成员"))
        lines.append(f"{nickname}({uid}) [{role}]")
    return "\n".join(lines)


async def _fn_get_member_profile(args, ctx=None, event=None, user_id=None):
    """AI 函数：查询某人在群内的身份（角色/昵称/头衔，含插件注入身份说明）"""
    gid = str(args.get("group_id") or
               (event.group_id if event and getattr(event, "is_group", False) else "") or "")
    uid = str(args.get("user_id") or "")
    if not gid or not uid:
        return "错误：缺少 group_id 或 user_id 参数"
    data = await _aapi("get_group_member_info", ttl=_API_CACHE_TTL,
                       group_id=_safe_int(gid), user_id=_safe_int(uid), no_cache=True)
    if not isinstance(data, dict):
        return f"错误：无法获取群 {gid} 中 {uid} 的身份信息"
    role_map = {"owner": "群主", "admin": "管理员", "member": "普通成员"}
    card = data.get("card") or data.get("nickname") or str(uid)
    role = role_map.get(str(data.get("role") or ""), str(data.get("role") or "成员"))
    title = data.get("title") or "无"
    lines = [
        f"QQ: {uid}",
        f"昵称/群名片: {card}",
        f"群内身份: {role}",
        f"专属头衔: {title}",
    ]
    if data.get("join_time"):
        lines.append(f"入群时间: {time.strftime('%Y-%m-%d', time.localtime(int(data['join_time'])))}")
    ids = get_llm_identity_parts()
    if ids:
        lines.append("插件注入身份说明: " + "；".join(ids))
    return "\n".join(lines)


async def _build_group_context(event, user_id):
    """构建群上下文文本：群信息 + 机器人自身身份 + 说话人身份 + 插件注入身份。

    这是注入到 system prompt 的上下文（不是 AI 可调用函数），全部走异步 aapi + 缓存。
    私聊时仅附插件注入身份说明。
    """
    is_group = bool(event and getattr(event, "is_group", False) and event.group_id)
    parts = []
    if is_group:
        gid = str(event.group_id)
        # 群信息（缓存 60s）
        group_info = await _aapi("get_group_info", ttl=_API_CACHE_TTL, group_id=_safe_int(gid))
        if isinstance(group_info, dict):
            gname = group_info.get("group_name") or gid
            parts.append(f"当前群: {gname}（群号 {gid}）")
        else:
            parts.append(f"当前群号: {gid}")
        # 机器人自身身份（缓存 60s）
        bot_self = await _aapi("get_login_info", ttl=_API_CACHE_TTL)
        bot_qq = str((bot_self or {}).get("user_id") or "") if isinstance(bot_self, dict) else ""
        if bot_qq:
            bot_info = await _aapi("get_group_member_info", ttl=_API_CACHE_TTL,
                                   group_id=_safe_int(gid), user_id=_safe_int(bot_qq), no_cache=True)
            if isinstance(bot_info, dict):
                role_map = {"owner": "群主", "admin": "管理员", "member": "普通成员"}
                parts.append(
                    f"你的身份: 机器人QQ={bot_qq}，群内角色="
                    f"{role_map.get(str(bot_info.get('role') or ''), bot_info.get('role') or '成员')}，"
                    f"昵称={bot_info.get('card') or bot_info.get('nickname') or bot_qq}，"
                    f"专属头衔={bot_info.get('title') or '无'}")
        # 说话人身份（缓存 60s）
        if user_id:
            member = await _aapi("get_group_member_info", ttl=_API_CACHE_TTL,
                                 group_id=_safe_int(gid), user_id=_safe_int(user_id), no_cache=True)
            if isinstance(member, dict):
                role_map = {"owner": "群主", "admin": "管理员", "member": "普通成员"}
                parts.append(
                    f"当前与你对话的人: QQ={user_id}，"
                    f"昵称={member.get('card') or member.get('nickname') or user_id}，"
                    f"群内角色={role_map.get(str(member.get('role') or ''), member.get('role') or '成员')}，"
                    f"专属头衔={member.get('title') or '无'}")
            else:
                parts.append(f"当前与你对话的人: QQ={user_id}（未能获取群名片）")
    # 插件注入的身份说明
    ids = get_llm_identity_parts()
    if ids:
        parts.append("插件注入的身份说明: " + "；".join(ids))
    if not parts:
        return ""
    return "\n\n【当前上下文】\n" + "\n".join(parts)


# ================= 工具调用执行 =================

def _call_handler_sync(handler, args, event, user_id):
    """同步 handler 执行（供异步包装调用）"""
    try:
        sig = inspect.signature(handler)
        n_params = len([
            p for p in sig.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ])
    except (TypeError, ValueError):
        n_params = 1
    if n_params <= 1:
        return handler(args)
    return handler(args, ctx, event, user_id)


async def _call_handler(handler, args, event, user_id):
    """调用注册的函数 handler，兼容同步/异步与不同参数个数"""
    if inspect.iscoroutinefunction(handler):
        try:
            sig = inspect.signature(handler)
            n_params = len([
                p for p in sig.parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ])
        except (TypeError, ValueError):
            n_params = 1
        if n_params <= 1:
            return await handler(args)
        return await handler(args, ctx, event, user_id)
    # 同步 handler 丢线程池，避免阻塞事件循环
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _call_handler_sync, handler, args, event, user_id)


# ================= WebUI 数据快照（函数列表 / 调用日志 / 统计 / 人格） =================

def _tool_log_max() -> int:
    try:
        v = int(_get_config("tool_log_max", _TOOL_LOGS_MAX_DEFAULT) or _TOOL_LOGS_MAX_DEFAULT)
        return max(10, min(v, 1000))
    except Exception:
        return _TOOL_LOGS_MAX_DEFAULT


def _log_tool_call(name, args, user_id, group_id, ok, result, cost, err=""):
    """记录一次函数调用日志（供 WebUI 展示），并更新统计"""
    try:
        entry = {
            "time": _now_str(),
            "ts": time.time(),
            "name": name,
            "args": args,
            "user_id": str(user_id or ""),
            "group_id": str(group_id or ""),
            "ok": bool(ok),
            "result": str(result)[:600],
            "cost": round(float(cost), 2),
            "error": str(err)[:300],
        }
        with _TOOL_LOGS_LOCK:
            _TOOL_LOGS.insert(0, entry)
            mx = _tool_log_max()
            if len(_TOOL_LOGS) > mx:
                del _TOOL_LOGS[mx:]
            with _STATS_LOCK:
                _STATS["tool_calls"] += 1
                if not ok:
                    _STATS["tool_errors"] += 1
    except Exception as e:
        _log_warn(f"记录函数调用日志失败: {e}")
    _schedule_snapshot()


def _bump_chat_stat():
    """每次成功对话 +1 统计"""
    try:
        with _STATS_LOCK:
            _STATS["chats"] += 1
    except Exception:
        pass


def _collect_webui_data() -> dict:
    """收集 WebUI 需要的数据（函数列表/调用日志/统计/人格/配置）"""
    funcs = []
    try:
        with _LLM_FUNCTIONS_LOCK:
            for fname, item in _LLM_FUNCTIONS.items():
                funcs.append({
                    "name": fname,
                    "description": str(item.get("description") or ""),
                    "parameters": item.get("parameters") or {},
                    "plugin_name": str(item.get("plugin_name") or ""),
                })
        funcs.sort(key=lambda x: x["name"])
    except Exception as e:
        _log_warn(f"收集函数列表失败: {e}")
    logs = []
    try:
        with _TOOL_LOGS_LOCK:
            logs = list(_TOOL_LOGS)
    except Exception:
        pass
    personas = []
    cur_persona = ""
    try:
        personas = ctx.db_query(
            f"SELECT id, name, prompt, enabled, created_at FROM {_PERSONA_TABLE} ORDER BY id", ()) or []
        cur_persona = str(_get_config("current_persona", "") or "")
    except Exception as e:
        _log_warn(f"读取人格列表失败: {e}")
    stats = {}
    try:
        with _STATS_LOCK:
            stats = dict(_STATS)
    except Exception:
        pass
    config = {}
    try:
        config = ctx.get_all_config()
    except Exception:
        pass
    return {
        "generated_at": _now_str(),
        "stats": stats,
        "functions": funcs,
        "tool_logs": logs,
        "personas": personas,
        "current_persona": cur_persona,
        "config": config,
    }


def _write_webui_snapshot():
    """把运行时数据写入 data/plugins_dat/llm_chat/_webui_data.json（框架 API 可读）"""
    try:
        data = _collect_webui_data()
        path = os.path.join(ctx.get_data_dir(), "_webui_data.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, path)
    except Exception as e:
        _log_warn(f"WebUI 快照写入失败: {e}")


def _schedule_snapshot():
    """异步写快照（不阻塞对话流程；写入失败不影响主流程）"""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(asyncio.to_thread(_write_webui_snapshot))
    except Exception:
        try:
            import threading as _th
            _th.Thread(target=_write_webui_snapshot, daemon=True).start()
        except Exception:
            pass


async def _call_tool(name, args, event, user_id):
    """执行一个工具调用，返回结果文本（含详细日志 + WebUI 调用日志）"""
    with _LLM_FUNCTIONS_LOCK:
        item = _LLM_FUNCTIONS.get(name)
    if not item:
        _log_warn(f"工具调用: {name} 不存在")
        _log_tool_call(name, args, user_id,
                       event.group_id if event and event.is_group else "",
                       False, "函数不存在", 0, "函数不存在")
        return f"错误：函数 {name} 不存在"
    handler = item["handler"]
    t0 = time.time()
    _log_info(f"工具调用开始: {name} args={json.dumps(args, ensure_ascii=False)[:300]} "
              f"group={event.group_id if event.is_group else '-'} user={user_id}")
    try:
        result = await _call_handler(handler, args, event, user_id)
        if result is None:
            result = ""
        cost = time.time() - t0
        _log_info(f"工具调用完成: {name} 耗时={cost:.2f}s 结果={str(result)[:300]}")
        _log_tool_call(name, args, user_id,
                       event.group_id if event and event.is_group else "",
                       True, result, cost)
        return str(result)
    except Exception as e:
        cost = time.time() - t0
        _log_err(f"工具执行异常: {name} 耗时={cost:.2f}s", e)
        _log_tool_call(name, args, user_id,
                       event.group_id if event and event.is_group else "",
                       False, "", cost, str(e))
        return f"错误：函数 {name} 执行异常: {e}"


# ================= LLM 对话核心 =================

async def _post_json(client, url, headers, payload):
    """带重试的 POST，返回响应对象（含详细日志）"""
    last_exc = None
    for attempt in range(3):
        try:
            return await client.post(url, headers=headers, json=payload)
        except Exception as e:
            last_exc = e
            if attempt < 2:
                _log_warn(f"POST 失败(第{attempt+1}次重试) {url}: {e}")
                await asyncio.sleep(1 if attempt == 0 else 3)
    raise last_exc


async def _chat_once(messages: list, event=None, user_id=None):
    """调用 OpenAI 兼容接口，支持工具调用循环，返回 (回复文本, 工具轮数)"""
    import httpx  # 懒加载
    base_url = _base_url()
    if not base_url:
        raise RuntimeError("未配置 base_url，请在 Web UI → 插件配置 中设置 LLM 接口")

    url = base_url + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    api_key = str(_get_config("api_key", "")).strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": _model(),
        "messages": messages,
        "temperature": float(_get_config("temperature", 0.7) or 0.7),
        "max_tokens": int(_get_config("max_tokens", 2048) or 2048),
    }
    tools = get_llm_function_schemas()
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    max_rounds = _max_tool_rounds()
    _log_info(f"LLM请求开始: url={url} model={payload['model']} tools={len(tools)} "
              f"messages={len(messages)} temp={payload['temperature']} max_tokens={payload['max_tokens']} "
              f"group={event.group_id if event and event.is_group else '-'} user={user_id}")
    t_req = time.time()
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        for round_i in range(max_rounds + 1):
            resp = await _post_json(client, url, headers, payload)
            if resp.status_code != 200:
                raise RuntimeError(f"LLM 接口返回 HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            # usage 日志（排查 token 消耗）
            usage = data.get("usage") or {}
            _log_info(f"LLM响应 第{round_i+1}轮: HTTP 200 "
                      f"prompt_tokens={usage.get('prompt_tokens', '?')} "
                      f"completion_tokens={usage.get('completion_tokens', '?')} "
                      f"total_tokens={usage.get('total_tokens', '?')} "
                      f"耗时={time.time()-t_req:.2f}s")
            try:
                msg = data["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as e:
                raise RuntimeError(f"LLM 响应解析失败: {e}")

            content = str(msg.get("content") or "").strip()
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return content, round_i

            # 有工具调用：把 assistant 消息（含 tool_calls）加入对话，再逐个执行
            messages.append(msg)
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "")
                args_raw = str(fn.get("arguments") or "{}")
                try:
                    args = json.loads(args_raw or "{}")
                    if not isinstance(args, dict):
                        args = {"value": args}
                except Exception:
                    args = {"raw": args_raw}
                result = await _call_tool(name, args, event, user_id)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or "",
                    "content": result,
                })
            # 下一轮让模型基于工具结果继续作答
        # 达到轮数上限仍无最终回答
        _log_warn(f"达到最大工具轮数 {max_rounds}，未生成最终回答")
        return "（已达到最大工具调用轮数，未生成最终回答）", max_rounds


async def _send(event, message):
    """发送消息；若含分段占位符 <dvi>（可配），自动拆段逐条发送，段间随机延迟 1~3 秒"""
    message = str(message or "")
    placeholder = str(_get_config("segment_placeholder", "<dvi>") or "<dvi>")
    if placeholder and placeholder in message:
        try:
            dmin = max(0.0, float(_get_config("segment_delay_min", 1) or 1))
            dmax = max(dmin, float(_get_config("segment_delay_max", 3) or 3))
        except (TypeError, ValueError):
            dmin, dmax = 1.0, 3.0
        segments = [s.strip() for s in message.split(placeholder) if s.strip()]
        if not segments:
            segments = [message]
        _log_info(f"分段回复: 共 {len(segments)} 段，段间延迟 {dmin}~{dmax}s")
        for i, seg in enumerate(segments):
            await ctx.asend_msg(
                user_id=event.user_id,
                group_id=event.group_id if event.is_group else None,
                message=seg,
            )
            if i < len(segments) - 1:
                await asyncio.sleep(random.uniform(dmin, dmax))
        return
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=message,
    )


def _clip(text, limit=4000):
    text = str(text or '')
    return text if len(text) <= limit else text[:limit] + "...（内容过长已截断）"


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


async def _do_chat(event, user_id: str, text: str):
    """对话核心逻辑（/chat 与艾特触发共用）"""
    # 群/私聊开关
    if event.is_group and not _cfg_bool("enable_group_chat", True):
        return
    if not event.is_group and not _cfg_bool("enable_private_chat", True):
        return
    low = text.lower().strip()
    if low in ("clear", "清空", "重置", "new", "新会话"):
        _SESSIONS.pop(user_id, None)
        await _send(event, "🧹 已清空当前会话上下文，开始新一轮对话。")
        return

    if not text:
        await _send(event,
                    "请发送要对话的内容，例如：/chat 你好，或直接 @机器人 说内容\n"
                    "可用命令：/chat clear 清空上下文、/chat new 新开一轮")
        return

    # 黑名单拦截（预防上下文滥用）
    if _is_blacklisted(user_id):
        tip = str(_get_config("blacklist_tip", "")).strip() or (
            "你已被加入 LLM 黑名单，暂时无法使用对话功能。")
        _log_info(f"黑名单拦截 user={user_id}")
        await _send(event, tip)
        return

    group_tag = str(event.group_id) if event.is_group else "-"
    _log_info(f"收到对话请求: user={user_id} group={group_tag} text={_clip(text, 100)}")

    history = _get_history(user_id)
    # 每次对话刷新系统上下文（含最新群上下文/身份注入/好感度/重要记忆），保证动态信息不过期
    sys_prompt = _default_prompt()
    gctx = await _build_group_context(event, user_id)
    if gctx:
        sys_prompt += gctx
    # 好感度注入（可配置开关）
    if str(_get_config("affinity_inject", True)).lower() in ("true", "1", "yes", "on"):
        v = _get_affinity_value(user_id)
        if v is not None:
            sys_prompt += (f"\n\n【好感度】你对 {user_id} 的好感度: {v}（等级: {_affinity_level(v)}）。"
                           f"好感度高时更亲近热情，低时更冷淡克制。")
    # 重要记忆注入（最多配置条数）
    mem_block = _important_memories_block(user_id)
    if mem_block:
        sys_prompt += "\n\n" + mem_block

    if history and history[0].get("role") == "system":
        history[0]["content"] = sys_prompt
    else:
        history.insert(0, {"role": "system", "content": sys_prompt})
    # 写入用户消息到历史库（内部含写入检测 detect_on_write，违规不入库）
    await _save_history(user_id, event.group_id if getattr(event, "is_group", False) else None,
                        "user", text)
    history.append({"role": "user", "content": _clip(text, 2000)})

    # 限制历史轮数（保留 system + 最近 N*2 条）
    max_rounds = int(_get_config("max_history_rounds", 10) or 10)
    if len(history) > 1 + max_rounds * 2:
        history[:] = [history[0]] + history[-(max_rounds * 2):]

    # 静默请求（不发送"思考中"提示）
    t0 = time.time()
    try:
        reply, tool_rounds = await _chat_once(list(history), event, user_id)
    except Exception as e:
        history.pop()  # 失败时回滚用户消息，避免污染上下文
        _log_err(f"LLM 对话失败 user={user_id} group={group_tag} text={_clip(text, 100)}", e)
        await _send(event, f"⚠️ 请求失败：{e}")
        return
    cost = time.time() - t0

    # 请求日志上报：时间/群组/QQ/请求摘要/回复摘要/工具轮数/耗时
    _log_info(
        f"[LLM上报] time={_now_str()} group={group_tag} user={user_id} "
        f"rounds={tool_rounds} cost={cost:.1f}s "
        f"req={_clip(text, 100)} resp={_clip(reply, 150)}")

    if not reply:
        history.pop()
        await _send(event, "⚠️ LLM 返回了空内容，请重试。")
        return

    history.append({"role": "assistant", "content": reply})
    # AI 回复安全检测：违规 → 不入库、不发送，改发拦截提示（detect_on_send 开关控制）
    if _detect_on_send():
        try:
            r = await _check_content(reply)
            if not r["ok"]:
                _log_warn(f"AI 回复未通过安全检测，已拦截 user={user_id} group={group_tag} reason={r['reason']}")
                await _send(event, _detect_block_tip())
                return
        except Exception as e:
            _log_warn(f"AI 回复安全检测异常(放行): {e}")
    # 写入 AI 回复到历史库（内部含写入检测 detect_on_write，违规不入库）
    await _save_history(user_id, event.group_id if getattr(event, "is_group", False) else None,
                        "assistant", reply)
    # 成功对话 → 好感度 +1（可配置关闭）
    try:
        step = int(_get_config("affinity_step", 1) or 1)
    except (TypeError, ValueError):
        step = 1
    if step > 0:
        _add_affinity(user_id, step)
    _bump_chat_stat()

    max_reply = int(_get_config("max_reply_chars", 3500) or 3500)
    await _send(event, _clip(reply, max_reply))


async def handle_chat(event, match):
    """/chat <内容> 对话主处理"""
    if not _cfg_bool("enable_chat_command", True):
        await _send(event, "🔕 对话指令已关闭，请直接 @机器人 对话。")
        return
    user_id = str(event.user_id)
    text = ""
    if match:
        text = (match.group(1) or "").strip()
    if not text:
        msg = event.message or ""
        for prefix in ("/chat", "/对话", "/ai聊天", "/gpt"):
            if msg.startswith(prefix):
                text = msg[len(prefix):].strip()
                break
    await _do_chat(event, user_id, text)


async def handle_at(event, match):
    """艾特机器人触发对话（@机器人 + 内容）"""
    if not event.has_at_bot:
        return False  # 不是艾特机器人，放行继续路由
    if not _cfg_bool("enable_at_trigger", True):
        return True  # 艾特触发已关闭，吞掉本次消息（不回复）
    text = re.sub(r"\[@\d+\]", " ", event.message or "").strip()
    await _do_chat(event, str(event.user_id), text)

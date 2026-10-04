# -*- coding: utf-8 -*-
"""
关键词引擎核心模块
==================
负责规则数据的存储与缓存、命中匹配、三类动作的执行细节：
- 自动回复动作（reply）：按作用域（全局优先、本群在后）匹配，返回回复内容
- 外部 API 动作（api）：渲染参数模板、发起 GET/POST 请求、按路径提取响应内容
- 监控通知动作（notify）：包含匹配监控词，由调用方私聊通知超级管理员

规则统一存放在 keyword_rules 表（经 ctx.create_table 创建，自动适配数据库方言）：
    规则 = 触发（关键词 / 正则） + 动作（reply / api / notify）
         + 作用域（群号字符串 / '0' 表示全局） + 启用状态 + 命中计数

其中 API 动作规则会额外同步到框架的 dynamic_commands 表（动态命令），
消息未命中插件命令时由路由按匹配方式兜底触发 keyword:_kwapi_dyn_handler 调用外部接口。
"""
import json
import re
import threading
import time
import urllib.parse
import urllib.request

import requests

ctx = None  # 框架上下文，由 init() 注入

GLOBAL_SCOPE = "0"          # 全局作用域：scope 字段为 "0" 表示对所有群生效
ACTION_REPLY = "reply"      # 动作类型：自动回复
ACTION_API = "api"          # 动作类型：调用外部 API
ACTION_NOTIFY = "notify"    # 动作类型：私聊通知超级管理员
VALID_MATCH = ("exact", "prefix", "contains", "regex")

DYN_CMD_PLUGIN = "keyword"                            # dynamic_commands 表中的插件标识
DYN_CMD_HANDLER = "keyword:_kwapi_dyn_handler"        # 动态命令回调规格（插件:函数）

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS keyword_rules (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    scope         VARCHAR(32)   DEFAULT '0',
    keyword       VARCHAR(191)  NOT NULL,
    match_type    VARCHAR(20)   DEFAULT 'exact',
    is_regex      TINYINT(1)    DEFAULT 0,
    action_type   VARCHAR(20)   DEFAULT 'reply',
    reply_content TEXT,
    api_url       VARCHAR(1000) DEFAULT '',
    method        VARCHAR(10)   DEFAULT 'GET',
    params        TEXT,
    headers       TEXT,
    success_path  VARCHAR(500)  DEFAULT '',
    prefix        VARCHAR(200)  DEFAULT '',
    is_enabled    TINYINT(1)    DEFAULT 1,
    created_by    VARCHAR(50)   DEFAULT '',
    created_at    VARCHAR(32)   DEFAULT '',
    hit_count     INT           DEFAULT 0
)
"""

_FORBIDDEN_DEFAULT_API = "https://api-v2.yuafeng.cn/API/wjc.php"  # 违禁词检测接口默认地址

_lock = threading.RLock()
_cache = []        # 规则内存缓存（命中匹配走内存，写操作后强制刷新）
_cache_ts = 0.0


# ================= 基础 =================

def conf(key, default=None):
    """读取 Web UI 配置（带默认值兜底）"""
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


def init(plugin_ctx):
    """插件注册入口调用：注入上下文、建表、加载规则缓存"""
    global ctx
    ctx = plugin_ctx
    try:
        ctx.create_table(_TABLE_DDL)
    except Exception as e:
        ctx.log(f"规则表创建失败: {e}", level="error")
    _refresh(force=True)


def extract_text(message) -> str:
    """从 OneBot 消息提取纯文本（兼容字符串与消息段数组两种形态，剥离图片/at 等非文本段）"""
    if isinstance(message, list):
        parts = []
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append(str((seg.get("data") or {}).get("text", "")))
        return "".join(parts)
    return str(message or "")


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ================= 规则缓存 =================

def _row_to_rule(r) -> dict:
    """数据库行 → 规则字典（统一字段类型）"""
    return {
        "id": int(r.get("id", 0) or 0),
        "scope": str(r.get("scope") if r.get("scope") is not None else GLOBAL_SCOPE),
        "keyword": str(r.get("keyword") or ""),
        "match_type": (r.get("match_type") or "exact").strip().lower(),
        "is_regex": 1 if int(r.get("is_regex", 0) or 0) else 0,
        "action_type": (r.get("action_type") or ACTION_REPLY).strip().lower(),
        "reply_content": r.get("reply_content") or "",
        "api_url": r.get("api_url") or "",
        "method": (r.get("method") or "GET").strip().upper(),
        "params": r.get("params") or "",
        "headers": r.get("headers") or "",
        "success_path": r.get("success_path") or "",
        "prefix": r.get("prefix") or "",
        "is_enabled": 1 if int(r.get("is_enabled", 1) or 0) else 0,
        "created_by": str(r.get("created_by") or ""),
        "created_at": str(r.get("created_at") or ""),
        "hit_count": int(r.get("hit_count", 0) or 0),
    }


def _refresh(force=False) -> list:
    """从数据库加载规则到内存缓存（TTL 控制刷新频率）"""
    global _cache, _cache_ts
    ttl = float(conf("cache_ttl", 30) or 30)
    now = time.time()
    if not force and _cache and (now - _cache_ts) < ttl:
        return _cache
    try:
        rows = ctx.db_query("SELECT * FROM keyword_rules ORDER BY id ASC")
        rules = [_row_to_rule(r) for r in rows]
        with _lock:
            _cache = rules
            _cache_ts = now
        return rules
    except Exception as e:
        ctx.log(f"规则加载失败: {e}", level="error")
        with _lock:
            return _cache


def load_rules(force=False) -> list:
    """获取全部规则（内存缓存）"""
    return _refresh(force=force)


def api_rules() -> list:
    """全部 API 动作规则（强制刷新，供动态命令同步与列表展示）"""
    return [r for r in _refresh(force=True) if r["action_type"] == ACTION_API]


# ================= 匹配 =================

def _match_one(rule, text) -> bool:
    """单条规则匹配（按匹配方式分派；正则编译异常兜底跳过）"""
    kw = str(rule.get("keyword", "")).strip()
    if not kw:
        return False
    mt = rule.get("match_type")
    try:
        if mt == "prefix":
            return text.startswith(kw)
        if mt == "contains":
            return kw in text
        if mt == "regex":
            return re.search(kw, text) is not None
        return text == kw  # exact：完全一致
    except re.error:
        return False


def match_reply_rules(gid, text) -> list:
    """自动回复动作匹配：全局规则优先、本群规则在后，返回命中的启用规则"""
    rules = _refresh()
    gid = str(gid)
    matched = []
    for scope in (GLOBAL_SCOPE, gid):
        for r in rules:
            if not r["is_enabled"] or r["action_type"] != ACTION_REPLY or r["scope"] != scope:
                continue
            if _match_one(r, text):
                matched.append(r)
    return matched


def match_notify_rules(gid, text) -> list:
    """监控通知动作匹配：监控词包含在消息文本中即命中"""
    rules = _refresh()
    gid = str(gid)
    return [r for r in rules
            if r["is_enabled"] and r["action_type"] == ACTION_NOTIFY
            and r["scope"] in (GLOBAL_SCOPE, gid)
            and r["keyword"] and r["keyword"] in text]


def bump_hits(rule_ids):
    """命中计数 +1（数据库落库，并同步内存缓存）"""
    ids = [int(i) for i in rule_ids if i]
    if not ids:
        return
    ph = ",".join(["%s"] * len(ids))
    try:
        ctx.db_execute(
            f"UPDATE keyword_rules SET hit_count = hit_count + 1 WHERE id IN ({ph})",
            tuple(ids))
    except Exception as e:
        ctx.log(f"命中计数更新失败: {e}", level="warning")
    with _lock:
        for r in _cache:
            if r["id"] in ids:
                r["hit_count"] = int(r.get("hit_count", 0)) + 1


# ================= 规则增删改查 =================

def reply_rules_in_scope(scope) -> list:
    """某作用域下的全部自动回复规则（直接读库，保证列表/统计准确）"""
    try:
        rows = ctx.db_query(
            "SELECT * FROM keyword_rules WHERE scope = %s AND action_type = %s ORDER BY id ASC",
            (str(scope), ACTION_REPLY))
        return [_row_to_rule(r) for r in rows]
    except Exception as e:
        ctx.log(f"规则查询失败: {e}", level="error")
        return []


def upsert_reply_rule(scope, keyword, content, is_regex, created_by) -> int:
    """新增/更新自动回复规则：同作用域同关键词已存在则更新内容（保持启用状态），否则新增"""
    mt = "regex" if is_regex else "exact"
    rows = ctx.db_query(
        "SELECT id FROM keyword_rules WHERE scope = %s AND keyword = %s AND action_type = %s",
        (str(scope), keyword, ACTION_REPLY))
    if rows:
        rid = int(rows[0]["id"])
        ctx.db_execute(
            "UPDATE keyword_rules SET reply_content = %s, is_regex = %s, match_type = %s "
            "WHERE id = %s",
            (content, 1 if is_regex else 0, mt, rid))
    else:
        rid = int(ctx.db_insert(
            "INSERT INTO keyword_rules "
            "(scope, keyword, match_type, is_regex, action_type, reply_content, "
            " is_enabled, created_by, created_at, hit_count) "
            "VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, 0)",
            (str(scope), keyword, mt, 1 if is_regex else 0, ACTION_REPLY,
             content, str(created_by), _now_str())))
    _refresh(force=True)
    return rid


def delete_reply_rule(scope, keyword) -> int:
    """删除某作用域下的自动回复规则（含正则），返回受影响行数"""
    n = ctx.db_execute(
        "DELETE FROM keyword_rules WHERE scope = %s AND keyword = %s AND action_type = %s",
        (str(scope), keyword, ACTION_REPLY))
    _refresh(force=True)
    return n


def set_reply_enabled(scope, keyword, enabled) -> int:
    """启用/禁用某作用域下的自动回复规则，返回受影响行数"""
    n = ctx.db_execute(
        "UPDATE keyword_rules SET is_enabled = %s "
        "WHERE scope = %s AND keyword = %s AND action_type = %s",
        (1 if enabled else 0, str(scope), keyword, ACTION_REPLY))
    _refresh(force=True)
    return n


def get_rule_by_id(rid):
    """按 id 取规则详情；不存在返回 None"""
    try:
        rows = ctx.db_query("SELECT * FROM keyword_rules WHERE id = %s", (int(rid),))
        return _row_to_rule(rows[0]) if rows else None
    except Exception as e:
        ctx.log(f"规则查询失败: {e}", level="error")
        return None


def delete_rule_by_id(rid) -> int:
    """按 id 删除规则，返回受影响行数"""
    n = ctx.db_execute("DELETE FROM keyword_rules WHERE id = %s", (int(rid),))
    _refresh(force=True)
    return n


def set_rule_enabled_by_id(rid, enabled) -> int:
    """按 id 启用/禁用规则，返回受影响行数"""
    n = ctx.db_execute(
        "UPDATE keyword_rules SET is_enabled = %s WHERE id = %s",
        (1 if enabled else 0, int(rid)))
    _refresh(force=True)
    return n


def add_api_rule(keyword, match_type, api_url, method, params, headers,
                 success_path, prefix, creator) -> int:
    """新增 API 动作规则（全局作用域），返回新规则 id"""
    rid = int(ctx.db_insert(
        "INSERT INTO keyword_rules "
        "(scope, keyword, match_type, is_regex, action_type, api_url, method, "
        " params, headers, success_path, prefix, is_enabled, created_by, created_at, hit_count) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, 0)",
        (GLOBAL_SCOPE, keyword, match_type, 1 if match_type == "regex" else 0, ACTION_API,
         api_url, method, params, headers, success_path, prefix,
         str(creator), _now_str())))
    _refresh(force=True)
    return rid


def add_notify_rule(keyword, creator) -> int:
    """新增监控词（全局作用域、包含匹配）；监控词唯一，重复返回 0"""
    rows = ctx.db_query(
        "SELECT id FROM keyword_rules WHERE keyword = %s AND action_type = %s",
        (keyword, ACTION_NOTIFY))
    if rows:
        return 0
    rid = int(ctx.db_insert(
        "INSERT INTO keyword_rules "
        "(scope, keyword, match_type, is_regex, action_type, is_enabled, created_by, created_at, hit_count) "
        "VALUES (%s, %s, 'contains', 0, %s, 1, %s, %s, 0)",
        (GLOBAL_SCOPE, keyword, ACTION_NOTIFY, str(creator), _now_str())))
    _refresh(force=True)
    return rid


def remove_notify_rule(keyword) -> int:
    """移除监控词，返回受影响行数"""
    n = ctx.db_execute(
        "DELETE FROM keyword_rules WHERE keyword = %s AND action_type = %s",
        (keyword, ACTION_NOTIFY))
    _refresh(force=True)
    return n


def notify_keywords() -> list:
    """当前全部监控词（排序去重）"""
    return sorted({r["keyword"] for r in _refresh()
                   if r["action_type"] == ACTION_NOTIFY and r["keyword"]})


def superuser_ids() -> list:
    """全部超级管理员用户 id"""
    try:
        rows = ctx.db_query("SELECT user_id FROM users WHERE role = 'super'")
        return [int(r["user_id"]) for r in rows]
    except Exception:
        return []


# ================= 动态命令同步（API 动作） =================

def sync_dynamic_commands():
    """把 API 动作规则同步为框架动态命令（dynamic_commands 表），实现统一管理：
    - WebUI 命令管理 → 关键词回复 分组可见/启停
    - 消息未命中插件命令时，路由按匹配方式兜底触发 _kwapi_dyn_handler 调用外部接口
    - 规则的启用/禁用/增删与 keyword_rules 表保持一致
    """
    try:
        rules = api_rules()
        # 全量重建本插件的动态命令（dynamic_commands 无 keyword 唯一约束）
        ctx.db_execute("DELETE FROM dynamic_commands WHERE plugin_name = %s",
                       (DYN_CMD_PLUGIN,))
        for r in rules:
            ctx.db_insert(
                "INSERT INTO dynamic_commands "
                "(keyword, response, match_type, handler, plugin_name, is_active) "
                "VALUES (%s, '', %s, %s, %s, %s)",
                (r["keyword"], r["match_type"], DYN_CMD_HANDLER, DYN_CMD_PLUGIN,
                 1 if r["is_enabled"] else 0))
        # 让路由表重建（新规则热生效）
        try:
            ctx._framework.router._invalidate_cache()
        except Exception:
            pass
    except Exception as e:
        ctx.log(f"同步动态命令失败: {e}", level="error")


def kwapi_dyn_handler(rule, message):
    """动态命令兜底触发入口（被框架路由调用）
    签名：func(rule, message) -> 回复文本 | None
    根据关键词找到 keyword_rules 中完整的 API 规则 → 调用外部接口 → 返回回复文本
    """
    try:
        if not conf("api_enable", True):
            return None
        kw = getattr(rule, "keyword", None) or (rule.get("keyword") if isinstance(rule, dict) else "")
        for r in load_rules():
            if r["keyword"] == kw and r["action_type"] == ACTION_API and r["is_enabled"]:
                content = call_api_sync(r, message)
                if not content:
                    return None
                prefix = (r.get("prefix") or "").strip()
                return (prefix + content) if prefix else content
    except Exception as e:
        ctx.log(f"动态命令回调异常: {e}", level="error")
    return None


# ================= 外部 API 调用（API 动作） =================

_URL_RE = re.compile(r"https?://[^\s]+")


def _extract_urls(text):
    """提取消息中的所有 URL（文本+URL 混合模式用）"""
    return _URL_RE.findall(text or "")


def _render_template(tpl, arg="", msg="", url="", urls=""):
    """渲染参数/请求头模板：
      {arg}  = 关键词后剩余内容    {msg}  = 整条消息
      {url}  = 消息中第一个 URL    {urls} = 消息中所有 URL（空格分隔）
    """
    if not tpl:
        return ""
    s = str(tpl)
    s = s.replace("{arg}", arg or "")
    s = s.replace("{msg}", msg or "")
    s = s.replace("{url}", url or "")
    s = s.replace("{urls}", urls or "")
    return s


def _extract_arg(rule, text):
    """提取关键词后的剩余内容（供 {arg} 模板）"""
    mt = rule["match_type"]
    kw = rule["keyword"]
    if mt == "exact":
        return ""
    if mt == "prefix":
        return text[len(kw):].strip()
    if mt == "regex":
        try:
            m = re.search(kw, text)
            if m and m.groups():
                return m.group(1).strip()
        except re.error:
            pass
    return text.strip()


def _do_http(rule, params, headers, timeout):
    """发起 HTTP 请求（在子线程中执行）"""
    url = rule["api_url"]
    method = rule["method"]
    if method == "POST":
        return requests.post(url, json=params if params else None,
                             headers=headers or None, timeout=timeout)
    return requests.get(url, params=params if params else None,
                        headers=headers or None, timeout=timeout)


def _extract_content(resp, rule):
    """从 API 响应中提取回复内容：
    1. 配置了提取路径（如 data.content / data.list.0.text）→ 沿路径提取
    2. 响应是 JSON → 序列化为字符串
    3. 其他 → 返回原始文本
    """
    text = (resp.text or "").strip()
    if not text:
        return None
    data = None
    try:
        data = resp.json()
    except Exception:
        data = None

    path = (rule.get("success_path") or "").strip()
    if path and data is not None:
        val = data
        for part in path.split("."):
            part = part.strip()
            if not part:
                continue
            if isinstance(val, dict) and part in val:
                val = val[part]
            elif isinstance(val, list) and part.isdigit() and int(part) < len(val):
                val = val[int(part)]
            else:
                val = None
                break
        if val is not None:
            if isinstance(val, (dict, list)):
                return json.dumps(val, ensure_ascii=False)
            return str(val)
        return None  # 提取路径未命中 → 不回复

    if data is not None:
        if isinstance(data, (dict, list)):
            return json.dumps(data, ensure_ascii=False)
        return str(data)
    return text


def call_api_sync(rule, text):
    """调用规则绑定的外部 API，返回要发送的内容；失败返回 None（阻塞，需在子线程调用）"""
    if not rule.get("api_url"):
        return None
    timeout = float(conf("timeout", 10) or 10)
    arg = _extract_arg(rule, text)
    urls = _extract_urls(text)
    url = urls[0] if urls else ""

    # 参数模板：JSON 字符串 → dict（GET 当 query，POST 当 body）
    # 文本+URL 混合模式：{arg}=文本, {url}/{urls}=提取的URL
    params_tpl = _render_template(rule.get("params"), arg=arg, msg=text,
                                  url=url, urls=" ".join(urls)).strip()
    params = None
    if params_tpl:
        try:
            params = json.loads(params_tpl)
        except Exception:
            params = params_tpl  # 非 JSON → 原样字符串

    headers = None
    headers_tpl = _render_template(rule.get("headers"), arg=arg, msg=text,
                                   url=url, urls=" ".join(urls)).strip()
    if headers_tpl:
        try:
            headers = json.loads(headers_tpl)
        except Exception:
            headers = None

    try:
        resp = _do_http(rule, params, headers, timeout)
        if resp.status_code >= 400:
            ctx.log(f"API 错误 {resp.status_code}: {rule['api_url']}", level="warning")
            return None
        return _extract_content(resp, rule)
    except Exception as e:
        ctx.log(f"API 调用失败 [{rule['api_url']}]: {e}", level="warning")
        return None


# ================= 违禁词检测（添加规则时可选） =================

def check_forbidden_sync(text):
    """违禁词检测：返回命中的提示文本；未启用 / 通过 / 接口异常时返回 None（放行）"""
    if not conf("forbidden_check_enabled", False):
        return None
    if not text or not str(text).strip():
        return None
    api_url = str(conf("forbidden_api_url", _FORBIDDEN_DEFAULT_API) or _FORBIDDEN_DEFAULT_API).strip()
    if not api_url:
        return None
    params = {"text": str(text)[:500]}
    key = str(conf("forbidden_api_key", "") or "").strip()
    if key:
        params["key"] = key  # 接口需要密钥时随请求发送
    try:
        req = urllib.request.Request(api_url + "?" + urllib.parse.urlencode(params),
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        if isinstance(data, dict) and data.get("code") == 1:
            return str(data.get("filtered_text") or text)
    except Exception as e:
        ctx.log(f"违禁词检测接口异常: {e}", level="warning")
    return None

"""
关键词API自动回复插件
- 超级管理员通过 QQ 命令添加「关键词 → API」规则
- 任何人触发关键词（完全匹配/前缀/包含/正则）时，自动调用 API，把返回内容发到群里/私聊
- API 支持 GET/POST、参数模板 {arg}/{msg}、JSON 提取路径、回复前缀
- 依赖框架新特性：未命中命令的纯文本消息会广播 message 事件（需框架更新后生效）

用法（超管）：
  /关键词api                查看帮助
  /关键词api 列表           查看规则列表
  /关键词api 添加 <关键词> <API地址> [--匹配 exact|prefix|contains|regex] [--方法 GET|POST] [--参数 '{"q":"{arg}"}'] [--提取 data.content] [--前缀 回复前缀]
  /关键词api 删除 <id>
  /关键词api 启用 <id> / 禁用 <id>
  /关键词api 测试 <id> [内容]   手动模拟触发（不走消息事件）
"""
import asyncio
import json
import re
import threading
import time

import requests

__plugin_meta__ = {
    "name": "关键词API",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "超管添加关键词→API规则，触发关键词自动调API并回复内容",
    "priority": 90,
}

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS keyword_api_rules (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    keyword      VARCHAR(191) NOT NULL,
    match_type   VARCHAR(20) DEFAULT 'exact',
    api_url      VARCHAR(1000) NOT NULL,
    method       VARCHAR(10) DEFAULT 'GET',
    params       TEXT,
    headers      TEXT,
    success_path VARCHAR(500) DEFAULT '',
    prefix       VARCHAR(200) DEFAULT '',
    enabled      TINYINT(1) DEFAULT 1,
    creator      VARCHAR(50) DEFAULT '',
    created_at   VARCHAR(32) DEFAULT ''
)
"""

_VALID_MATCH = ('exact', 'prefix', 'contains', 'regex')

ctx = None
_rules_cache = []            # [{id, keyword, match_type, api_url, method, params, headers, success_path, prefix, enabled}]
_rules_cache_ts = 0.0
_cache_lock = threading.Lock()


def register(ctx_arg):
    """插件注册入口"""
    global ctx
    ctx = ctx_arg
    _ensure_table()
    _refresh_rules(force=True)
    ctx.command(
        "/关键词api", cmd_main,
        priority=40,
        require_superuser=True,
        alias=["/kwapi"],
        description="关键词API规则管理（超管）：添加/删除/列表/启用/禁用/测试",
    )
    # 规则统一注册为框架动态命令（dynamic_commands 表）：
    # WebUI 命令管理 → 关键词回复 可见/启停；路由在插件未命中时兜底触发（_dyn_handler 调 API）
    try:
        _sync_dynamic_commands()
    except Exception as e:
        ctx.log(f"[keyword_api] 初始同步动态命令失败: {e}", level="warning")
    ctx.log("[keyword_api] 关键词API插件已注册（动态命令兜底触发 + 超管命令）", level="info")


def on_error(event, error):
    try:
        ctx.log(f"[keyword_api] handler 异常: {error}", level="error")
    except Exception:
        pass


# ================= 配置 =================

def _get_config(key, default=None):
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


# ================= 数据库 =================

def _ensure_table():
    try:
        ctx.create_table(_TABLE_DDL)
    except Exception:
        # 兼容旧框架（无 create_table 方法）：直接用 db_execute（内部自动翻译方言）
        try:
            ctx.db_execute(_TABLE_DDL)
        except Exception as e:
            ctx.log(f"[keyword_api] 建表失败: {e}", level="error")


def _row_to_rule(r):
    return {
        'id': r['id'],
        'keyword': r.get('keyword') or '',
        'match_type': (r.get('match_type') or 'exact').strip().lower(),
        'api_url': r.get('api_url') or '',
        'method': (r.get('method') or 'GET').upper(),
        'params': r.get('params') or '',
        'headers': r.get('headers') or '',
        'success_path': r.get('success_path') or '',
        'prefix': r.get('prefix') or '',
        'enabled': int(r.get('enabled', 1) or 1),
    }


def _refresh_rules(force=False):
    """从数据库加载规则到内存缓存（TTL 控制刷新频率）"""
    global _rules_cache, _rules_cache_ts
    ttl = float(_get_config('cache_ttl', 30) or 30)
    now = time.time()
    if not force and (now - _rules_cache_ts) < ttl:
        return _rules_cache
    try:
        rows = ctx.db_query("SELECT * FROM keyword_api_rules ORDER BY id ASC")
        rules = [_row_to_rule(r) for r in rows]
        with _cache_lock:
            _rules_cache = rules
            _rules_cache_ts = now
        return rules
    except Exception as e:
        ctx.log(f"[keyword_api] 加载规则失败: {e}", level="error")
        return _rules_cache


def _get_rules():
    return _refresh_rules()


def _sync_dynamic_commands():
    """
    把 keyword_api 规则同步为框架动态命令（dynamic_commands 表），实现统一管理：
    - WebUI 命令管理 → 关键词回复 可见（plugin_name=keyword_api）
    - 路由在插件命令未命中时兜底触发 → _dyn_handler 调 API 回复
    - 规则的启用/禁用/增删与 keyword_api_rules 表保持一致
    """
    try:
        rules = _refresh_rules(force=True)
        # 全量重建 keyword_api 的动态命令（dynamic_commands 无 keyword 唯一约束）
        ctx.db_execute("DELETE FROM dynamic_commands WHERE plugin_name='keyword_api'")
        for r in rules:
            ctx.db_insert(
                "INSERT INTO dynamic_commands "
                "(keyword, response, match_type, handler, plugin_name, is_active) "
                "VALUES (%s, '', %s, 'keyword_api:_dyn_handler', 'keyword_api', %s)",
                (r['keyword'], r['match_type'], 1 if r['enabled'] else 0))
        # 让路由表重建（新规则热生效）
        try:
            ctx._framework.router._invalidate_cache()
        except Exception:
            pass
    except Exception as e:
        ctx.log(f"[keyword_api] 同步动态命令失败: {e}", level="error")


def _dyn_handler(rule, message):
    """
    动态命令兜底触发入口（被框架 router._call_keyword_handler 调用）
    签名：func(rule, message) -> 回复文本 | None
    根据规则 keyword 找到 keyword_api_rules 完整配置（api_url 等）→ 调 API → 返回文本
    """
    try:
        kw = getattr(rule, 'keyword', None) or (rule.get('keyword') if isinstance(rule, dict) else '')
        for r in _get_rules():
            if r['keyword'] == kw and r['enabled']:
                content = _call_api_sync(r, message)
                if not content:
                    return None
                prefix = (r.get('prefix') or '').strip()
                return (prefix + content) if prefix else content
    except Exception as e:
        ctx.log(f"[keyword_api] _dyn_handler 异常: {e}", level="error")
    return None


def _rule_by_id(rule_id):
    for r in _get_rules():
        if int(r['id']) == int(rule_id):
            return r
    return None


# ================= 匹配 =================

def _match_rule(text):
    """遍历规则找第一条命中的（顺序按 id 升序）"""
    for r in _get_rules():
        if not r['enabled']:
            continue
        kw = r['keyword']
        if not kw:
            continue
        mt = r['match_type']
        try:
            if mt == 'exact':
                if text == kw:
                    return r
            elif mt == 'prefix':
                if text.startswith(kw):
                    return r
            elif mt == 'contains':
                if kw in text:
                    return r
            elif mt == 'regex':
                if re.search(kw, text):
                    return r
        except re.error:
            continue
    return None


def _extract_arg(rule, text):
    """提取关键词后的剩余内容（供 {arg} 模板）"""
    mt = rule['match_type']
    kw = rule['keyword']
    if mt == 'exact':
        return ''
    if mt == 'prefix':
        return text[len(kw):].strip()
    if mt == 'regex':
        try:
            m = re.search(kw, text)
            if m and m.groups():
                return m.group(1).strip()
        except re.error:
            pass
    return text.strip()


# ================= API 调用 =================

_URL_RE = re.compile(r'https?://[^\s]+')


def _extract_urls(text):
    """提取消息中的所有 URL（文本+URL 混合模式用）"""
    return _URL_RE.findall(text or '')


def _render_template(tpl, arg='', msg='', url='', urls=''):
    """
    渲染参数模板（文本 + URL 混合模式）：
      {arg}  = 关键词后剩余内容（含文本与 URL 的原样）
      {msg}  = 整条消息
      {url}  = 消息中的第一个 URL（无则空）
      {urls} = 消息中所有 URL（空格分隔，无则空）
    """
    if not tpl:
        return ''
    s = str(tpl)
    s = s.replace('{arg}', arg or '')
    s = s.replace('{msg}', msg or '')
    s = s.replace('{url}', url or '')
    s = s.replace('{urls}', urls or '')
    return s


def _do_http(rule, params, headers, timeout):
    """同步 HTTP 请求（在子线程中执行）"""
    url = rule['api_url']
    method = rule['method']
    if method == 'POST':
        return requests.post(
            url, json=params if params else None,
            headers=headers or None, timeout=timeout)
    return requests.get(
        url, params=params if params else None,
        headers=headers or None, timeout=timeout)


def _extract_content(resp, rule):
    """从 API 响应中提取回复内容：
    1. 配置了 success_path（如 data.content / data.list.0.text）→ 沿路径提取
    2. 响应是 JSON → 序列化为字符串
    3. 其他 → 返回原始文本
    """
    text = (resp.text or '').strip()
    if not text:
        return None
    data = None
    try:
        data = resp.json()
    except Exception:
        data = None

    path = (rule.get('success_path') or '').strip()
    if path and data is not None:
        val = data
        for part in path.split('.'):
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


async def _call_api(rule, text):
    """调用规则绑定的 API，返回要发送的内容；失败返回 None（异步版）"""
    return await asyncio.to_thread(_call_api_sync, rule, text)


def _call_api_sync(rule, text):
    """调用规则绑定的 API，返回要发送的内容；失败返回 None（同步版）"""
    if not rule['api_url']:
        return None
    timeout = float(_get_config('timeout', 10) or 10)
    arg = _extract_arg(rule, text)
    urls = _extract_urls(text)
    url = urls[0] if urls else ''

    # 参数模板：JSON 字符串 → dict（GET 当 query，POST 当 body）
    # 文本+URL 混合模式：{arg}=文本, {url}/{urls}=提取的URL
    params_tpl = _render_template(rule['params'], arg=arg, msg=text, url=url, urls=' '.join(urls)).strip()
    params = None
    if params_tpl:
        try:
            params = json.loads(params_tpl)
        except Exception:
            params = params_tpl  # 非 JSON → 原样字符串

    headers = None
    headers_tpl = _render_template(rule['headers'], arg=arg, msg=text, url=url, urls=' '.join(urls)).strip()
    if headers_tpl:
        try:
            headers = json.loads(headers_tpl)
        except Exception:
            headers = None

    try:
        resp = _do_http(rule, params, headers, timeout)
        if resp.status_code >= 400:
            ctx.log(f"[keyword_api] API 错误 {resp.status_code}: {rule['api_url']}", level="warning")
            return None
        content = _extract_content(resp, rule)
        return content
    except Exception as e:
        ctx.log(f"[keyword_api] API 调用失败 [{rule['api_url']}]: {e}", level="warning")
        return None


# ================= 触发处理 =================

async def _on_message(ev):
    """message 事件：未命中任何命令的纯文本消息，检查关键词规则"""
    if not _get_config('enabled', True):
        return False
    text = (ev.message or '').strip()
    if not text:
        return False
    # 管理命令自己处理（虽然已被命令路由拦截，这里兜底防御）
    if text.startswith('/关键词api') or text.startswith('/kwapi'):
        return False

    rule = _match_rule(text)
    if rule is None:
        return False

    content = await _call_api(rule, text)
    if not content:
        return False

    max_len = int(_get_config('max_reply_len', 2000) or 2000)
    prefix = rule.get('prefix') or ''
    reply = (prefix + content)[:max_len]

    target = {'group_id': ev.group_id} if ev.is_group else {'user_id': ev.user_id}
    try:
        await ctx.asend_msg(**target, message=reply)
        ctx.log(f"[keyword_api] 触发规则 #{rule['id']} 「{rule['keyword']}」→ 已回复", level="info")
        return True
    except Exception as e:
        ctx.log(f"[keyword_api] 发送失败: {e}", level="error")
        return False


# ================= 管理命令 =================

def cmd_main(event, match):
    """超管管理命令：/关键词api <子命令>"""
    # 权限防御（框架已校验 super，这里再兜底）
    if not event.is_superuser:
        return False
    args = (match.group(1) if match else '').strip()
    parts = args.split(maxsplit=1)
    sub = parts[0] if parts else ''
    rest = parts[1] if len(parts) > 1 else ''

    target = {'group_id': event.group_id} if event.is_group else {'user_id': event.user_id}

    if not sub or sub in ('帮助', 'help', 'h', '?'):
        _send(target, _HELP)
        return True

    if sub in ('列表', 'list', 'ls'):
        _send(target, _do_list())
        return True

    if sub in ('添加', 'add', '新增'):
        _send(target, _do_add(rest, event.user_id))
        return True

    if sub in ('删除', 'del', 'delete', 'remove'):
        _send(target, _do_delete(rest))
        return True

    if sub in ('启用', 'enable', 'on'):
        _send(target, _do_set_enabled(rest, 1))
        return True

    if sub in ('禁用', 'disable', 'off'):
        _send(target, _do_set_enabled(rest, 0))
        return True

    if sub in ('测试', 'test'):
        _send(target, _do_test(rest))
        return True

    _send(target, f"未知子命令: {sub}\n\n{_HELP}")
    return True


def _send(target, text):
    try:
        ctx.send_msg(**target, message=text)
    except Exception as e:
        ctx.log(f"[keyword_api] 发送失败: {e}", level="error")


_HELP = (
    "🔑 关键词API 管理（超管）\n"
    "添加规则：/关键词api 添加 <关键词> <API地址> [--匹配 exact|prefix|contains|regex] [--方法 GET|POST] [--参数 '{\"q\":\"{arg}\"}'] [--提取 data.content] [--前缀 回复前缀]\n"
    "查看列表：/关键词api 列表\n"
    "删除规则：/关键词api 删除 <id>\n"
    "启用/禁用：/关键词api 启用 <id> ｜ /关键词api 禁用 <id>\n"
    "手动测试：/关键词api 测试 <id> [内容]\n"
    "参数模板：{arg}=关键词后内容 ｜ {msg}=整条消息\n"
    "示例：/关键词api 添加 天气 https://api.example.com/weather --参数 '{\"city\":\"{arg}\"}' --提取 data.weather"
)


def _do_add(rest, creator):
    """解析：<关键词> <API地址> [--匹配 x] [--方法 x] [--参数 'json'] [--提取 path] [--前缀 text]"""
    # 位置参数：关键词、API地址（遇 -- 开头停止）
    tokens = rest.split()
    if len(tokens) < 2:
        return "用法：/关键词api 添加 <关键词> <API地址> [--匹配 ...] [--方法 ...] [--参数 ...] [--提取 ...] [--前缀 ...]"
    keyword = tokens[0]
    api_url = tokens[1]

    # 可选参数（--xxx 值 或 --xxx=值）
    opts = {'匹配': 'exact', '方法': 'GET', '参数': '', '提取': '', '前缀': ''}
    i = 2
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith('--'):
            key = tok[2:].lstrip('-')
            if '=' in key:
                k, v = key.split('=', 1)
                opts.setdefault(k, v)
            else:
                k = key
                val = tokens[i + 1] if i + 1 < len(tokens) else ''
                opts[k] = val
                i += 1
        i += 1

    mt = (opts.get('匹配') or 'exact').strip().lower()
    if mt not in _VALID_MATCH:
        return f"匹配方式无效: {mt}（可选 {', '.join(_VALID_MATCH)}）"
    method = (opts.get('方法') or 'GET').upper()
    if method not in ('GET', 'POST'):
        return f"方法无效: {method}（可选 GET/POST）"

    try:
        rid = ctx.db_insert(
            "INSERT INTO keyword_api_rules "
            "(keyword, match_type, api_url, method, params, headers, success_path, prefix, enabled, creator, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s)",
            (keyword, mt, api_url, method,
             opts.get('参数') or '', opts.get('headers') or '',
             opts.get('提取') or '', opts.get('前缀') or '',
             str(creator), time.strftime('%Y-%m-%d %H:%M:%S')),
        )
        _refresh_rules(force=True)
        _sync_dynamic_commands()
        return (f"✅ 已添加规则 #{rid}\n"
                f"关键词：{keyword}（{mt}）\n"
                f"API：{api_url}（{method}）\n"
                f"提取路径：{opts.get('提取') or '自动'}\n"
                f"前缀：{opts.get('前缀') or '无'}\n"
                f"（已注册为动态命令，命令管理页可见）")
    except Exception as e:
        ctx.log(f"[keyword_api] 添加规则失败: {e}", level="error")
        return f"❌ 添加失败: {e}"


def _do_list():
    rules = _get_rules()
    if not rules:
        return "📋 暂无规则，用 /关键词api 添加 <关键词> <API地址> 创建"
    lines = ["📋 关键词API 规则列表："]
    for r in rules:
        status = "✅" if r['enabled'] else "⛔"
        lines.append(
            f"{status} #{r['id']} [{r['match_type']}] {r['keyword']}\n"
            f"    ↳ {r['method']} {r['api_url'][:60]}"
        )
    return "\n".join(lines)


def _do_delete(rest):
    rid = rest.strip()
    if not rid.isdigit():
        return "用法：/关键词api 删除 <id>"
    n = ctx.db_execute("DELETE FROM keyword_api_rules WHERE id = %s", (int(rid),))
    _refresh_rules(force=True)
    _sync_dynamic_commands()
    return f"✅ 已删除规则 #{rid}" if n else f"❌ 规则 #{rid} 不存在"


def _do_set_enabled(rest, enabled):
    rid = rest.strip()
    if not rid.isdigit():
        return "用法：/关键词api 启用|禁用 <id>"
    n = ctx.db_execute(
        "UPDATE keyword_api_rules SET enabled = %s WHERE id = %s",
        (enabled, int(rid)),
    )
    _refresh_rules(force=True)
    _sync_dynamic_commands()
    if not n:
        return f"❌ 规则 #{rid} 不存在"
    return f"✅ 规则 #{rid} 已{'启用' if enabled else '禁用'}"


def _do_test(rest):
    """/关键词api 测试 <id> [内容]"""
    parts = rest.split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        return "用法：/关键词api 测试 <id> [内容]"
    rid = int(parts[0])
    text = parts[1].strip() if len(parts) > 1 else ''
    rule = _rule_by_id(rid)
    if rule is None:
        return f"❌ 规则 #{rid} 不存在"
    if not text:
        text = rule['keyword']

    content = _call_api_sync(rule, text)
    if not content:
        return f"❌ 测试失败（规则 #{rid}「{rule['keyword']}」API 未返回可用内容）"
    max_len = int(_get_config('max_reply_len', 2000) or 2000)
    reply = ((rule.get('prefix') or '') + content)[:max_len]
    return f"🧪 规则 #{rid} 测试结果（内容: {text}）:\n{reply}"

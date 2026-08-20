"""
个性化前端插件 (custom_ui)
=================================================
本插件负责从 GitHub 拉取网页模板（zip），并经 Flask 重定向提供服务，
同时提供管理 WebUI 让用户选择模板/主题并下载安装。

工作流：
1. 插件启用后，框架根路由 / redirect 到 /custom_ui/
2. /custom_ui/ 服务当前激活的模板网页（框架默认前端被取代）
3. 管理页 /custom_ui/manage 展示模板列表（来自插件仓库 GitHub webui/ 目录）
4. 用户选择模板 → 下载（拉取 zip 到本地 templates/）→ 安装/切换 → 刷新生效
5. 多模板并存，可随时切换；切换即更新当前激活标记

模板源：https://github.com/kuangxing6367/zcbot_plugins 仓库顶层 webui/ 目录
（每个模板一个 zip，zip 内为网页主体 index.html / css / js / img 等）
"""
import os
import re
import shutil
import tempfile
import zipfile

__plugin_meta__ = {
    "name": "个性化前端",
    "version": "1.1.0",
    "author": "ZGRIC",
    "desc": "从 GitHub 拉取网页模板接管 Web 面板：提供模板选择/下载/安装/切换",
    "priority": 250,
}

# 模板源仓库（与官方插件市场同仓库）
GITHUB_REPO = "kuangxing6367/zcbot_plugins"
GITHUB_BRANCH = "main"
TEMPLATES_DIR_NAME = "webui"  # 仓库顶层 webui/ 目录，存放模板 zip


# ---------------------------------------------------------------- 工具

# 数据目录（plugins_dat/custom_ui），register 时从 ctx 获取并缓存。
# 模板、激活标记、接管标记都存这里：插件更新/重装不会覆盖。
_DATA_DIR = None


def _plugin_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _dat_dir():
    """插件数据目录（plugins_dat/custom_ui），模板与状态标记存放处"""
    if _DATA_DIR:
        return _DATA_DIR
    # 未初始化时回退到代码目录（仅作 fallback）
    return _plugin_dir()


def _init_dat_dir(ctx_local):
    global _DATA_DIR
    try:
        _DATA_DIR = ctx_local.get_data_dir()
        os.makedirs(_DATA_DIR, exist_ok=True)
    except Exception:
        _DATA_DIR = _plugin_dir()
    return _DATA_DIR


def _templates_root():
    """本地模板存放根：plugins_dat/custom_ui/templates/<name>/<解压内容>"""
    return os.path.join(_dat_dir(), 'templates')


def _active_marker():
    """当前激活模板标记文件（存数据目录，更新不丢）"""
    return os.path.join(_dat_dir(), 'active.txt')


def _override_marker():
    """接管标记文件（存数据目录，更新不丢）"""
    return os.path.join(_dat_dir(), 'override.txt')


def _get_active_template():
    try:
        with open(_active_marker(), 'r', encoding='utf-8') as f:
            name = f.read().strip()
        if name and os.path.isdir(os.path.join(_templates_root(), name)):
            return name
    except Exception:
        pass
    return None


def _set_active_template(name):
    os.makedirs(_templates_root(), exist_ok=True)
    with open(_active_marker(), 'w', encoding='utf-8') as f:
        f.write(name)


def _is_override_enabled():
    try:
        with open(_override_marker(), 'r', encoding='utf-8') as f:
            return f.read().strip() == '1'
    except Exception:
        return False


def _set_override(enabled: bool):
    try:
        if enabled:
            with open(_override_marker(), 'w', encoding='utf-8') as f:
                f.write('1')
        else:
            if os.path.exists(_override_marker()):
                os.remove(_override_marker())
        return True
    except Exception:
        return False


def _log(ctx_local, msg, level='info'):
    try:
        ctx_local.log(f"[custom_ui] {msg}", level=level)
    except Exception:
        pass


def _safe_template_name(name):
    """只允许模板名：字母数字下划线连字符点"""
    m = re.match(r'^([A-Za-z0-9_\-\.]+)$', name or '')
    return m.group(1) if m else None


def _github_raw_candidates(path):
    """生成 GitHub raw 候选下载地址（加速代理 → 直连）"""
    base = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}"
    direct = f"{base}/{path}"
    cands = []
    for proxy in ("https://gh.jasonzeng.dev/https://", "https://ghproxy.net/https://"):
        cands.append(f"{proxy}{direct.lstrip('https://')}")
    cands.append(direct)
    return cands


def _http_get(url, timeout=30):
    import requests
    return requests.get(url, timeout=timeout)


def _list_remote_templates():
    """通过 GitHub API 列出仓库顶层 webui/ 目录下的模板 zip"""
    import requests
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{TEMPLATES_DIR_NAME}"
    headers = {'Accept': 'application/vnd.github.v3+json', 'User-Agent': 'zcbot-plugin'}
    try:
        resp = requests.get(api_url, headers=headers, timeout=20)
        if resp.status_code != 200:
            return [], f'GitHub API 返回 {resp.status_code}'
        items = resp.json()
        templates = []
        for it in items:
            if not isinstance(it, dict):
                continue
            name = it.get('name', '')
            if name.lower().endswith('.zip'):
                templates.append({
                    'name': name[:-4],        # 去掉 .zip
                    'file': name,
                    'size': it.get('size', 0),
                    'download_url': it.get('download_url', ''),
                })
        return templates, ''
    except Exception as e:
        return [], str(e)[:200]


def _download_template(ctx_local, tpl_name):
    """
    从 GitHub 下载指定模板 zip 并解压到 templates/<name>/。
    返回 (ok, msg)。
    """
    name = _safe_template_name(tpl_name)
    if not name:
        return False, '非法模板名'
    path = f"{TEMPLATES_DIR_NAME}/{name}.zip"
    ok = False
    last_err = ''
    for url in _github_raw_candidates(path):
        try:
            resp = _http_get(url, timeout=60)
            if resp.status_code != 200:
                last_err = f'HTTP {resp.status_code}'
                continue
            if resp.content[:2] != b'PK':
                last_err = '内容非 ZIP'
                continue
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
            try:
                tmp.write(resp.content)
            finally:
                tmp.close()
            target = os.path.join(_templates_root(), name)
            # 覆盖安装：先清空目标目录
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            os.makedirs(target, exist_ok=True)
            with zipfile.ZipFile(tmp.name, 'r') as z:
                z.extractall(target)
            os.unlink(tmp.name)
            ok = True
            break
        except Exception as e:
            last_err = str(e)[:150]
            continue
    if ok:
        _log(ctx_local, f"模板 [{name}] 下载并安装成功")
        return True, '模板已下载并安装'
    return False, last_err or '下载失败'


# ---------------------------------------------------------------- Flask 路由

def _register_routes(ctx_local):
    try:
        app = ctx_local._framework.web_server.app
        if app is None:
            _log(ctx_local, "web_server.app 为空，跳过路由注册", 'warning')
            return
        from flask import jsonify, redirect, request, send_from_directory
        existing = {str(r.rule) for r in app.url_map.iter_rules()}
        added = 0

        # 鉴权辅助：从 Authorization/cookie 取 token 并验证
        def _auth():
            auth = request.headers.get('Authorization', '')
            token = auth[7:] if auth.startswith('Bearer ') else request.cookies.get('zcbot_token')
            if not token:
                return None
            try:
                row = ctx_local._framework.db.query_one(
                    "SELECT id, username, role, is_active FROM admin_users WHERE token = %s",
                    (token,))
            except Exception:
                row = None
            if not row or not row.get('is_active'):
                return None
            return row

        # ---- 模板网页：/custom_ui/ 与 /custom_ui/<path> ----
        def _serve_template(path=''):
            """服务当前激活模板的网页。无激活模板时显示模板管理页。"""
            active = _get_active_template()
            if not active:
                return redirect('/custom_ui/manage', code=302)
            tpl_root = os.path.join(_templates_root(), active)
            if not path:
                # 默认入口
                if os.path.isfile(os.path.join(tpl_root, 'index.html')):
                    return send_from_directory(tpl_root, 'index.html')
                return redirect('/custom_ui/manage', code=302)
            # 防目录穿越
            norm = os.path.normpath(path)
            if norm.startswith('..') or norm.startswith('/') or '\\' in norm:
                return jsonify({'code': 400, 'msg': '非法路径'}), 400
            full = os.path.join(tpl_root, norm)
            if not os.path.isfile(full):
                return jsonify({'code': 404, 'msg': '资源不存在'}), 404
            return send_from_directory(tpl_root, norm)

        # ---- 管理页：/custom_ui/manage ----
        def _manage_page():
            manage_html = os.path.join(_plugin_dir(), 'manage.html')
            if os.path.isfile(manage_html):
                return send_from_directory(_plugin_dir(), 'manage.html')
            return "<h1>custom_ui 模板管理</h1><p>manage.html 缺失</p>"

        # ---- API ----
        def _api_templates():
            """模板列表：本地已下载 + GitHub 远端可用"""
            remote, err = _list_remote_templates()
            active = _get_active_template()
            local = []
            tpl_root = _templates_root()
            if os.path.isdir(tpl_root):
                for name in os.listdir(tpl_root):
                    if os.path.isdir(os.path.join(tpl_root, name)):
                        local.append(name)
            return jsonify({'code': 0, 'data': {
                'remote': remote,
                'local': local,
                'active': active,
                'error': err,
            }})

        def _api_download(tpl_name):
            """下载并安装模板"""
            if not _auth():
                return jsonify({'code': 401, 'msg': '未登录'}), 401
            ok, msg = _download_template(ctx_local, tpl_name)
            return jsonify({'code': 0 if ok else 500, 'msg': msg})

        def _api_activate(tpl_name):
            """切换激活模板"""
            if not _auth():
                return jsonify({'code': 401, 'msg': '未登录'}), 401
            name = _safe_template_name(tpl_name)
            if not name or not os.path.isdir(os.path.join(_templates_root(), name)):
                return jsonify({'code': 404, 'msg': '模板不存在（请先下载）'}), 404
            _set_active_template(name)
            _log(ctx_local, f"已切换到模板 [{name}]，刷新网页生效")
            return jsonify({'code': 0, 'msg': f'已切换到模板 [{name}]，刷新网页生效'})

        def _api_reset():
            """恢复框架默认前端：禁用本插件"""
            if not _auth():
                return jsonify({'code': 401, 'msg': '未登录'}), 401
            loader = ctx_local._framework.plugin_loader
            loader.unload_plugin(ctx_local.plugin_name)
            try:
                ctx_local._framework.db.execute(
                    "UPDATE plugins SET is_active = 0, status='stopped' WHERE plugin_name = %s",
                    (ctx_local.plugin_name,))
            except Exception:
                pass
            return jsonify({'code': 0, 'msg': '已恢复框架默认前端'})

        def _api_override():
            """启用前端接管（需已有激活模板）"""
            if not _auth():
                return jsonify({'code': 401, 'msg': '未登录'}), 401
            active = _get_active_template()
            if not active:
                return jsonify({'code': 400, 'msg': '请先下载并切换启用一个模板，再接管前端'}), 400
            if not _set_override(True):
                return jsonify({'code': 500, 'msg': '写入接管标记失败'}), 500
            ok = ctx_local.override_webui()
            _log(ctx_local, "已启用前端接管，根路由 redirect 到 /custom_ui/" if ok else "接管失败")
            return jsonify({'code': 0 if ok else 500,
                            'msg': '已启用前端接管，刷新网页生效' if ok else '接管失败'})

        def _api_cancel_override():
            """取消前端接管：回退框架默认 WebUI"""
            if not _auth():
                return jsonify({'code': 401, 'msg': '未登录'}), 401
            _set_override(False)
            loader = ctx_local._framework.plugin_loader
            loader.clear_override_webui(ctx_local.plugin_name)
            _log(ctx_local, "已取消前端接管，回退框架默认 WebUI")
            return jsonify({'code': 0, 'msg': '已取消接管，回退框架默认 WebUI'})

        rules = [
            ("/custom_ui/", ["GET"], _serve_template),
            ("/custom_ui/<path:path>", ["GET"], _serve_template),
            ("/custom_ui/manage", ["GET"], _manage_page),
            ("/api/custom_ui/templates", ["GET"], _api_templates),
            ("/api/custom_ui/templates/<tpl_name>/download", ["POST"], _api_download),
            ("/api/custom_ui/templates/<tpl_name>/activate", ["POST"], _api_activate),
            ("/api/custom_ui/reset", ["POST"], _api_reset),
            ("/api/custom_ui/override", ["POST"], _api_override),
            ("/api/custom_ui/override", ["DELETE"], _api_cancel_override),
        ]
        # 用底层 url_map.add() 注册路由，绕开 Flask 首次请求后 add_url_rule 的限制，
        # 保证插件热重载/重复加载时也能成功注册。
        from werkzeug.routing import Rule
        existing = {str(r.rule) for r in app.url_map.iter_rules()}
        for rule_path, methods, fn in rules:
            endpoint = f"custom_ui_{fn.__name__}"
            if rule_path in existing:
                # 已注册过：仅刷新 view 函数（重载后函数对象变化）
                app.view_functions[endpoint] = fn
                continue
            rule = Rule(rule_path, endpoint=endpoint, methods=methods)
            app.url_map.add(rule)
            app.view_functions[endpoint] = fn
            added += 1
        _log(ctx_local, f"路由注册完成（新增 {added} 条）")
    except Exception as e:
        _log(ctx_local, f"路由注册失败: {e}", 'error')


def register(ctx):
    """注册插件：不自动接管，仅在持久化标记启用且有激活模板时接管。"""
    # 初始化数据目录（模板/标记存 plugins_dat，更新不丢）
    _init_dat_dir(ctx)

    # 注册内嵌 WebUI 入口（框架 WebUI → 插件 WebUI 进入模板管理）
    ctx.webui("个性化前端", "index.html", icon="🎨", order=60)

    # 注册 API 路由（含接管/取消接管）
    _register_routes(ctx)

    # 若之前启用过接管且已有激活模板，恢复接管（重启保持）
    if _is_override_enabled():
        active = _get_active_template()
        if active:
            ok = ctx.override_webui()
            _log(ctx, "已恢复前端接管（标记启用，模板: " + active + "）" if ok else "恢复接管失败")
        else:
            _log(ctx, "接管标记已启用但无激活模板，暂不接管", 'warning')
            _set_override(False)
    else:
        _log(ctx, "未启用前端接管，框架默认 WebUI 照常")

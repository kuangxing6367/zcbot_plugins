"""
个性化前端插件 (custom_ui)
=================================================
让本插件接管整个 Web 管理面板：

- 插件提供 web/ 目录下的完整前端（index.html / css / js / img）
- 调用 ctx.override_webui() 后，框架根路由与静态资源全部从插件 web/ 目录服务
- 插件被禁用/卸载/删除时自动回退框架默认前端

前端内置能力：
- 刷新过快检测：5 秒内刷新 2 次 → 跳转到 /reset 恢复页
- /reset 恢复页：询问是否遇到前端问题 → 确认后要求登录 → 恢复前端默认设置
  （通过禁用本插件回到框架默认前端）
- 接管框架 config 管理：通过框架 /api/config/yaml 读写 config.yaml
"""

__plugin_meta__ = {
    "name": "个性化前端",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "接管整个 Web 管理面板，提供个性化前端（含刷新检测与 /reset 恢复页）",
    "priority": 250,
}


def _register_webui_routes(ctx_local):
    """向框架 Flask 应用动态注册 API 路由（reload 时幂等）"""
    try:
        app = ctx_local._framework.web_server.app
        if app is None:
            ctx_local.log("WebUI 路由注册失败: web_server.app 为空", level="warning")
            return

        existing = {str(r.rule) for r in app.url_map.iter_rules()}

        # ---- 恢复默认前端：禁用本插件（需登录）----
        def _reset_default():
            from flask import jsonify, request
            # 鉴权：与框架一致，从 Authorization 头或 cookie 取 token
            auth = request.headers.get('Authorization', '')
            token = auth[7:] if auth.startswith('Bearer ') else request.cookies.get('zcbot_token')
            if not token:
                return jsonify({'code': 401, 'msg': '未登录'}), 401
            row = None
            try:
                row = ctx_local._framework.db.query_one(
                    "SELECT id, username, role, is_active FROM admin_users WHERE token = %s",
                    (token,))
            except Exception:
                pass
            if not row or not row.get('is_active'):
                return jsonify({'code': 401, 'msg': '登录已失效'}), 401

            loader = ctx_local._framework.plugin_loader
            loader.unload_plugin(ctx_local.plugin_name)
            try:
                ctx_local._framework.db.execute(
                    "UPDATE plugins SET is_active = 0, status='stopped' WHERE plugin_name = %s",
                    (ctx_local.plugin_name,))
            except Exception:
                pass
            return jsonify({'code': 0, 'msg': '已恢复框架默认前端'})

        routes = [
            ("/api/custom_ui/reset", ["POST"], _reset_default),
        ]
        added = 0
        for rule, methods, fn in routes:
            if rule not in existing:
                app.add_url_rule(rule, endpoint=f"custom_ui_{fn.__name__}",
                                 view_func=fn, methods=methods)
                added += 1
        ctx_local.log(f"custom_ui API 路由注册完成（新增 {added} 条）")
    except Exception as e:
        ctx_local.log(f"custom_ui 路由注册失败: {e}", level="error")


def register(ctx):
    # 注册为前端接管插件：根路由与静态资源全部指向本插件 web/ 目录
    ok = ctx.override_webui()
    if ok:
        ctx.log("[custom_ui] 已接管 Web 前端，根路由指向插件 web/ 目录")
    else:
        ctx.log("[custom_ui] 接管前端失败（插件可能未正确加载）")

    # 附带注册一个内嵌 WebUI 入口（供框架插件管理页展示）
    ctx.webui("个性化前端", "index.html", icon="🎨", order=60)

    _register_webui_routes(ctx)
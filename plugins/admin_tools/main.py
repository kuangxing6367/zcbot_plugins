"""LLM 管群工具：把禁言/踢人注册成 llm_core 的 AI 函数，模型可在授权下协助群管"""
__plugin_meta__ = {
    "name": "LLM 管群工具",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "注册禁言/解禁/踢人三个 llm_core AI 函数，模型经权限校验后协助群管",
    "priority": 80,
}


def _is_admin(event, ctx):
    """工具执行前的权限校验：框架权限节点优先，事件角色兜底"""
    uid = getattr(event, "user_id", None) if event is not None else None
    try:
        if uid and ctx.has_perm(uid, "admin"):
            return True
    except Exception:
        pass
    return str(getattr(event, "role", "") or "") in ("admin", "owner", "super")


def _register(ctx):
    svc = ctx._framework.services.get("llm_core")
    if svc is None:
        raise RuntimeError("llm_core 服务未加载")

    @svc.tool(name="mute_group_member",
              description="禁言群成员。仅在用户明确要求禁言某人且具备管理权限时调用。",
              require_perm="admin", timeout=10)
    def mute_group_member(user_id: int, duration_seconds: int = 600, event=None):
        if not event or not getattr(event, "is_group", False):
            return "禁言失败：无法确定目标群（请在本群内发起）"
        if not _is_admin(event, ctx):
            return "禁言失败：你没有群管理权限"
        duration = max(0, min(int(duration_seconds), 30 * 24 * 3600))
        try:
            ctx.ban(event.group_id, int(user_id), duration)
            return "已禁言" if duration > 0 else "已解除禁言"
        except Exception as e:
            return f"禁言失败：{e}"

    @svc.tool(name="kick_group_member",
              description="将群成员移出群聊。这是重操作，仅在用户明确要求且具备管理权限时调用。",
              require_perm="admin", timeout=10)
    def kick_group_member(user_id: int, event=None):
        if not event or not getattr(event, "is_group", False):
            return "踢人失败：无法确定目标群（请在本群内发起）"
        if not _is_admin(event, ctx):
            return "踢人失败：你没有群管理权限"
        try:
            ctx.kick(event.group_id, int(user_id))
            return "已移出群聊"
        except Exception as e:
            return f"踢人失败：{e}"


def _try_register(ctx):
    try:
        _register(ctx)
        ctx.log("LLM 管群工具已注册到 llm_core（mute/kick）")
        return True
    except Exception as e:
        ctx.log(f"LLM 管群工具注册失败（llm_core 未加载?）: {e}", level="warning")
        return False


def register(ctx):
    if not _try_register(ctx):
        try:
            ctx.on("system.plugin.loaded", lambda _p: _try_register(ctx))
        except Exception:
            pass
    ctx.log("LLM 管群工具插件已注册")

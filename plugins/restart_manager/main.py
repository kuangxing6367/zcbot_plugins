"""
重启管理器插件
超级管理员发送 /重启 → 框架原地重启（os.execv，与 Web 面板「重启框架」同款）
重启完成后自动在发起会话（群/私聊）发送回执，并展示重启前后内存占用

设计要点：
- 待回执信息写入插件数据目录（get_data_dir()），重启后立即可读，不依赖数据库
- 插件 register 先于 WebSocket 启动，因此「重启完成」回执由后台线程轮询发送，
  每 5 秒重试一次，直到 WebSocket 恢复连接发送成功（最长等待 120 秒）
- 仅超级管理员可用（require_superuser=True），群聊/私聊均生效
"""
import json
import os
import sys
import threading
import time

__plugin_meta__ = {
    "name": "重启管理器",
    "version": "1.0.1",
    "author": "zgric",
    "desc": "超级管理员 /重启 原地重启框架，完成后回执内存占用",
    "priority": 50,
}

ctx = None

# 待回执文件（存于插件数据目录，重启后仍可读）
_PENDING_FILE = None
_MAX_WAIT = 120        # 重启完成后最多等待 120 秒发送回执
_RETRY_INTERVAL = 5    # 发送重试间隔（秒）


def _get_rss_mb() -> float:
    """读取当前进程 RSS（MB）：Linux 优先 /proc/self/status，兜底 resource"""
    try:
        with open('/proc/self/status', 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return round(int(line.split()[1]) / 1024, 1)
    except Exception:
        pass
    try:
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        return 0.0


def _main_py_path() -> str:
    """定位项目根目录的 main.py
    插件文件位于 plugins/<name>/main.py，向上三级即项目根目录：
      dirname(1) -> plugins/<name>
      dirname(2) -> plugins
      dirname(3) -> 项目根目录
    """
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'main.py'
    )


def _cleanup_pending():
    """删除待回执文件"""
    try:
        if _PENDING_FILE and os.path.isfile(_PENDING_FILE):
            os.remove(_PENDING_FILE)
    except Exception:
        pass


def _do_execv():
    """
    后台线程：稍等「正在重启」消息发出后，用 os.execv 原地替换进程重启框架
    与 framework/apis.py 的 /api/restart 逻辑一致，不依赖外部进程管理器
    """
    time.sleep(2)
    try:
        main_py = _main_py_path()
        # 入口校验：找不到 main.py 时绝不 execv（否则进程直接退出且无守护拉取）
        if not os.path.isfile(main_py):
            raise FileNotFoundError(f"框架入口不存在: {main_py}")
        python = sys.executable
        os.chdir(os.path.dirname(main_py))
        # sys.argv[1:] 保留启动时传入的 config 路径等参数
        os.execv(python, [python, main_py] + sys.argv[1:])
    except Exception as e:
        # execv 失败：尽力通知发起人并清理待回执，避免卡死
        ctx.log(f"[restart_manager] 重启失败: {e}", level='error')
        try:
            data = json.load(open(_PENDING_FILE, 'r', encoding='utf-8')) if _PENDING_FILE and os.path.isfile(_PENDING_FILE) else {}
            if data.get('group_id'):
                ctx.send_msg(group_id=data['group_id'], message=f"❌ 重启失败：{e}")
            elif data.get('user_id'):
                ctx.send_msg(user_id=data['user_id'], message=f"❌ 重启失败：{e}")
        except Exception:
            pass
        _cleanup_pending()


def _send_restart_done():
    """
    后台线程：重启后等待 WebSocket 连接恢复，发送完成回执（含重启前后内存对比）
    发送成功后删除待回执文件；超时仍未发出则清理并记日志
    """
    try:
        with open(_PENDING_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return

    after_rss = _get_rss_mb()
    before_rss = data.get('rss_mb', 0)
    group_id = data.get('group_id')
    user_id = data.get('user_id')
    operator = data.get('operator', '')

    message = (
        f"✅ 框架已重启完成\n"
        f"📊 内存占用：重启前 {before_rss} MB → 重启后 {after_rss} MB\n"
        f"👤 发起人：{operator}"
    )

    deadline = time.time() + _MAX_WAIT
    while time.time() < deadline:
        try:
            if group_id:
                ret = ctx.send_msg(group_id=group_id, message=message)
            else:
                ret = ctx.send_msg(user_id=user_id, message=message)
            # 发送成功：ret 为 dict 且 status=ok（未连接时为 failed，不抛异常）
            if isinstance(ret, dict) and ret.get('status') == 'ok':
                _cleanup_pending()
                return
        except Exception:
            pass
        time.sleep(_RETRY_INTERVAL)

    # 超时仍未发出（如机器人长时间未上线），清理文件并记录日志
    ctx.log("[restart_manager] 重启完成回执发送超时，已清理待回执文件", level='warning')
    _cleanup_pending()


def register(register_ctx):
    """插件注册入口"""
    global ctx, _PENDING_FILE
    ctx = register_ctx
    _PENDING_FILE = os.path.join(ctx.get_data_dir(), 'pending_restart.json')

    # 重启后恢复：若存在待回执文件，说明刚经历一次框架重启，启动回执发送线程
    if os.path.isfile(_PENDING_FILE):
        threading.Thread(target=_send_restart_done, daemon=True).start()

    ctx.command("/重启", _on_restart, priority=50, alias=["/restart"],
                description="重启框架（仅超级管理员）", require_superuser=True)


def _on_restart(event, match):
    """处理 /重启：记录会话 → 发送提示 → 后台线程执行原地重启"""
    group_id = event.group_id if event.is_group else None
    user_id = event.user_id if not event.is_group else None

    # 1. 写入待回执文件（重启后回执依据）
    try:
        payload = {
            "rss_mb": _get_rss_mb(),
            "group_id": group_id,
            "user_id": user_id,
            "operator": event.user_id,
            "time": time.time(),
        }
        with open(_PENDING_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception as e:
        ctx.log(f"[restart_manager] 写入待回执失败: {e}", level='error')

    # 2. 发送「正在重启」提示（同步桥接，发出后才继续）
    try:
        if group_id:
            ctx.send_msg(group_id=group_id, message="🔄 正在重启框架，请稍候...")
        else:
            ctx.send_msg(user_id=user_id, message="🔄 正在重启框架，请稍候...")
    except Exception:
        pass

    # 3. 后台线程执行重启（sleep 2s 确保提示已发出）
    threading.Thread(target=_do_execv, daemon=True).start()

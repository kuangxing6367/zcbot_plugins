# -*- coding: utf-8 -*-
"""
运行监控 (sysmon)
=================
统一运行监控套件：框架运行状态、系统资源、插件内存统计与进程内存诊断。

命令
----
  /status   框架运行状态概览（共享渲染引擎出图，失败回退文本）
  /info     系统详细信息（主机 / CPU / 内存 / 磁盘）
  /uptime   框架运行时间
  /plugins  已加载插件列表及版本描述
  /mem      各插件内存占用统计（估算值）
  /memdiag  重新采集内存诊断报告（含与上次快照的增量对比）

其他
----
  - 仪表盘卡片 2 张：系统状态卡（运行时长 + CPU/内存负载）、内存卡（进程内存 +
    插件内存 TOP，detail 携带完整统计供 WebUI 页面渲染）
  - WebUI 内嵌页 web/index.html：插件内存排行表格
  - 定时任务 2 个：每 5 分钟刷新插件内存统计缓存、清理系统信息缓存

内存统计原理：遍历各插件模块全局变量的对象图，递归 sys.getsizeof() 累加、
id() 去重防循环引用；对象引用共享会带来偏差，结果仅供参考。
"""
import os
import time
import traceback

import psutil

import sysmon_stats as stats
import sysmon_render as render

__plugin_meta__ = {
    "name": "运行监控",
    "version": "2.0.0",
    "author": "ZGRIC",
    "desc": "运行监控套件：框架状态/系统资源/插件内存统计与诊断，"
            "支持状态卡片图、仪表盘卡片与 WebUI 页面",
    "priority": 65,
}

# 框架上下文（register 时保存；命令/卡片/任务处理器经此访问框架能力）
ctx = None


def register(plugin_ctx):
    """插件注册入口：保存上下文并注册命令 / 定时任务 / 仪表盘卡片 / WebUI"""
    global ctx
    ctx = plugin_ctx

    # ── 命令 ──────────────────────────────────────────────
    ctx.command("/status", handle_status, priority=5,
                description="查看框架运行状态概览")
    ctx.command("/info", handle_info, priority=5,
                description="查看系统详细信息（CPU/内存/磁盘）")
    ctx.command("/uptime", handle_uptime, priority=5,
                description="查看框架运行时间")
    ctx.command("/plugins", handle_plugins, priority=10,
                description="查看已加载插件列表")
    ctx.command("/mem", handle_mem, priority=5,
                description="查看各插件内存占用统计")
    ctx.command("/memdiag", handle_memdiag, priority=999,
                description="重新采集内存诊断报告")

    # ── 定时任务（重统计放后台线程，避免挤占事件循环） ────
    ctx.task("*/5 * * * *", task_mem_refresh, description="刷新插件内存统计缓存")
    ctx.task("*/5 * * * *", task_sys_cleanup, description="清理系统状态缓存")

    # ── 仪表盘卡片 ────────────────────────────────────────
    ctx.dashboard_card("系统状态", _card_system_status, icon="⏱", priority=10)
    ctx.dashboard_card("内存", _card_memory, icon="📊", priority=15)

    # ── WebUI 内嵌页（web/index.html） ────────────────────
    ctx.webui("运行监控", "index.html", icon="📈", order=10)

    ctx.log("运行监控插件已注册")


# ══════════════════════════════════════════════════════════
#  命令处理：运行状态
# ══════════════════════════════════════════════════════════

def handle_status(event, match):
    """/status 框架运行状态概览（优先出状态卡片图，失败回退文本）"""
    fw = stats.get_framework_info(ctx)

    if 'error' in fw:
        ctx.api("send_msg",
                user_id=event.user_id,
                group_id=event.group_id,
                message=f"获取状态失败: {fw['error']}")
        return

    # 框架进程内存
    try:
        proc = psutil.Process(os.getpid())
        proc_mem = proc.memory_info().rss / 1024 / 1024
    except Exception:
        proc_mem = 0

    show_cpu = bool(ctx.get_config('show_cpu', True))
    sys_info = stats.get_system_info(ctx) if show_cpu else {}

    # 有共享渲染引擎且字体可用时出图，否则文本
    png = render.render_status_card(ctx, fw, proc_mem, sys_info, show_cpu)
    if png and render.send_png(ctx, event, png):
        return

    lines = [
        " 框架运行状态",
        "━━━━━━━━━━━━━━━",
        f"运行时间: {fw['uptime']}",
        f"进程内存: {proc_mem:.1f} MB",
    ]
    if show_cpu and 'error' not in sys_info:
        lines.append(f"CPU 使用率: {sys_info['cpu_percent']}% ({sys_info['cpu_count']}核)")
    lines.extend([
        "━━━━━━━━━━━━━━━",
        f"OneBot 客户端: {fw['bot_count']} 个在线",
        f"已加载插件: {fw['plugin_count']} 个",
        f"注册命令: {fw['cmd_count']} 条",
        f"动态命令: {fw['dyn_count']} 条",
        "━━━━━━━━━━━━━━━",
        f"用户数: {fw['user_count']}",
        f"活跃群: {fw['group_count']}",
        "━━━━━━━━━━━━━━━",
        f"WebSocket: :{fw['ws_port']}",
        f"Web UI: :{fw['web_port']}",
        f"发送 /info 查看系统详情",
    ])
    ctx.api("send_msg",
            user_id=event.user_id,
            group_id=event.group_id,
            message="\n".join(lines))


def handle_info(event, match):
    """/info 系统详细信息（主机 / CPU / 内存 / 可选磁盘）"""
    sys_info = stats.get_system_info(ctx)

    if 'error' in sys_info:
        ctx.api("send_msg",
                user_id=event.user_id,
                group_id=event.group_id,
                message=f"获取系统信息失败: {sys_info['error']}")
        return

    show_disk = bool(ctx.get_config('show_disk', True))

    lines = [
        " 系统信息",
        "━━━━━━━━━━━━━━━",
        f"主机名: {sys_info['hostname']}",
        f"系统: {sys_info['platform']}",
        f"Python: {sys_info['python']}",
        "━━━━━━━━━━━━━━━",
        f"CPU 使用率: {sys_info['cpu_percent']}%",
        f"CPU 核心数: {sys_info['cpu_count']}",
        "━━━━━━━━━━━━━━━",
        f"内存: {sys_info['mem_used']:.1f} / {sys_info['mem_total']:.1f} GB ({sys_info['mem_percent']}%)",
    ]
    if show_disk:
        lines.append(f"磁盘: {sys_info['disk_used']:.1f} / {sys_info['disk_total']:.1f} GB ({sys_info['disk_percent']}%)")
    lines.extend([
        "━━━━━━━━━━━━━━━",
        f"系统运行: {sys_info['boot_time']}",
    ])
    ctx.api("send_msg",
            user_id=event.user_id,
            group_id=event.group_id,
            message="\n".join(lines))


def handle_uptime(event, match):
    """/uptime 查看框架运行时间"""
    uptime = stats.format_uptime(time.time() - stats._plugin_start_time)
    ctx.api("send_msg",
            user_id=event.user_id,
            group_id=event.group_id,
            message=f"⏱ 框架已运行: {uptime}")


def handle_plugins(event, match):
    """/plugins 查看已加载插件列表（名称 / 版本 / 描述）"""
    fw = stats.get_framework_info(ctx)
    plugins = fw.get('plugins', [])

    if not plugins:
        ctx.api("send_msg",
                user_id=event.user_id,
                group_id=event.group_id,
                message="当前没有已加载的插件")
        return

    try:
        loaded_info = ctx._framework.plugin_loader.get_loaded_plugins()
    except Exception:
        loaded_info = {}

    lines = [f" 已加载插件 ({len(plugins)} 个)\n━━━━━━━━━━━━━━━"]
    for name in plugins:
        meta = (loaded_info.get(name) or {}).get('meta', {})
        version = meta.get('version', '?')
        desc = meta.get('desc', '')
        lines.append(f"• {name} v{version}\n  {desc}")

    ctx.api("send_msg",
            user_id=event.user_id,
            group_id=event.group_id,
            message="\n".join(lines))


# ══════════════════════════════════════════════════════════
#  命令处理：插件内存
# ══════════════════════════════════════════════════════════

def handle_mem(event, match):
    """/mem 查看各插件内存占用统计（强制重新统计，条形图 Top20）"""
    data = stats.get_mem_stats(ctx, force=True)
    plugins = data.get("plugins", [])

    lines = ["🧩 插件内存统计（估算值）", "━━━━━━━━━━━━━━━"]
    if not plugins:
        lines.append("暂无已加载插件数据")
    else:
        # 最多显示前 20 个，避免刷屏
        shown = plugins[:20]
        max_mem = max((p["mem_bytes"] for p in shown), default=1) or 1
        for p in shown:
            bar_len = max(1, int(p["mem_bytes"] / max_mem * 12))
            bar = "█" * bar_len
            lines.append(
                f"{p['name']:<8} {p['mem_mb']:>8.2f} MB {bar} "
                f"({p['obj_count']} 对象)"
            )
        if len(plugins) > 20:
            lines.append(f"…… 其余 {len(plugins) - 20} 个插件略")

    lines.extend([
        "━━━━━━━━━━━━━━━",
        f"插件估算合计: {data.get('total_mem_mb', 0)} MB",
        f"框架进程 RSS: {data.get('process_mem_mb', 0)} MB",
        f"刷新时间: {data.get('updated', '-')}",
    ])
    try:
        ctx.api("send_msg",
                user_id=event.user_id,
                group_id=event.group_id if event.is_group else None,
                message="\n".join(lines))
    except Exception as e:
        ctx.log(f"/mem 发送失败: {e}", level="error")


def handle_memdiag(event, match):
    """/memdiag 重新采集内存诊断报告（含与上次快照的增量对比）"""
    try:
        report, growth = stats.run_mem_diag(ctx)

        loaded = [k for k, v in report.get("heavy_libs", {}).items() if v.get("loaded")]
        top = report.get("top_maps", [])
        lines = [
            "🧠 内存诊断完成",
            f"RSS: {report.get('rss_mb')} MB | USS: {report.get('uss_mb')} MB",
            f"模块数: {report.get('module_count')}",
            f"已加载重型库: {', '.join(loaded) if loaded else '无'}",
        ]
        if growth:
            lines.append("📈 较上次快照增长 Top:")
            for gtype, delta_mb, cur_mb in growth:
                lines.append(f"  {gtype[:38]:<38} +{delta_mb:.2f} MB (当前 {cur_mb:.2f} MB)")
        else:
            lines.append("（首次诊断，下次 /memdiag 可对比增长）")
        for t in top[:3]:
            lines.append(f"  {t.get('path', '')[:40]}  {t.get('rss_mb')} MB")
        ctx.api("send_msg",
                user_id=event.user_id,
                group_id=event.group_id if event.is_group else None,
                message="\n".join(lines))
    except Exception as e:
        ctx.log(f"内存诊断失败: {e}\n{traceback.format_exc()}", level="error")


# ══════════════════════════════════════════════════════════
#  定时任务
# ══════════════════════════════════════════════════════════

def task_mem_refresh():
    """定时任务：强制刷新插件内存统计缓存（每 5 分钟）"""
    stats.refresh_mem_stats(ctx)


def task_sys_cleanup():
    """定时任务：清理系统信息缓存，让下次查询重新采集（每 5 分钟）"""
    stats.clear_system_cache()
    ctx.log("系统状态缓存已清理")


# ══════════════════════════════════════════════════════════
#  仪表盘卡片
# ══════════════════════════════════════════════════════════

def _card_system_status():
    """仪表盘卡片：系统状态（运行时长 + CPU/内存负载，只读轻量采集）"""
    try:
        cpu = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory().percent
        load = f"CPU {cpu}% / 内存 {mem}%"
    except Exception:
        load = "N/A"
    return {
        "value": stats.format_uptime(time.time() - stats._plugin_start_time),
        "label": f"{load} · 框架已运行",
        "icon": "⏱",
    }


def _card_memory():
    """仪表盘卡片：内存（进程 RSS + 插件内存 TOP，detail 供 WebUI 页面渲染）"""
    data = stats.get_mem_stats_cached()
    plugins = data.get("plugins", [])
    top = plugins[:3]
    if top:
        top_text = "  ".join(f"{p['name']} {p['mem_mb']}MB" for p in top)
    else:
        top_text = "暂无数据"
    return {
        "value": f"{data.get('process_mem_mb', 0):.1f} MB",
        "label": f"插件估算合计 {data.get('total_mem_mb', 0)} MB · TOP: {top_text}",
        "icon": "📊",
        "detail": data,  # WebUI 页面（web/index.html）数据源
    }


# ══════════════════════════════════════════════════════════
#  卸载清理
# ══════════════════════════════════════════════════════════

def on_unload():
    """插件卸载时清空缓存"""
    stats.clear_mem_cache()
    stats.clear_system_cache()
    try:
        ctx.log("运行监控插件已卸载")
    except Exception:
        pass

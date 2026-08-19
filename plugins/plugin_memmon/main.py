"""
插件内存监控 (plugin_memmon)
================================
检测框架中所有已加载插件（服务）的估算内存占用，并在 WebUI 展示。

统计原理
--------
Python 无法精确测量单个模块/插件的内存占用。本插件采用"对象图递归估算"：
遍历每个插件模块的全局变量（module.__dict__），递归调用 sys.getsizeof()
累加各对象占用，用 id() 去重防止循环引用重复计算，最终得到该插件的
估算内存与对象数量。结果仅供参考（对象引用共享会导致部分偏差）。

功能
----
1. /mem 命令：QQ 中查看各插件内存统计
2. 仪表盘卡片：进程总内存 + 插件内存明细
3. WebUI 独立页面：插件内存排行表格（进度条 / 对象数 / 函数数）
"""
import gc
import inspect
import json
import os
import sys
import time
import traceback
from collections import Counter, defaultdict

import psutil

__plugin_meta__ = {
    "name": "内存监控",
    "version": "1.1.0",
    "author": "ZGRIC",
    "desc": "插件内存统计 + 进程内存诊断（/mem 统计 /memdiag 诊断报告），WebUI 展示",
    "priority": 30,
}

# ── 统计参数 ──────────────────────────────────────────────
# 缓存有效期（秒），可在 WebUI 配置面板修改
_DEFAULT_TTL = 60
# 递归最大深度（防止过深递归导致卡死）
_MAX_DEPTH = 6
# 单次统计最大对象数（防止遍历到超大对象图导致卡死）
_MAX_OBJECTS = 500000

# ── 统计缓存 ──────────────────────────────────────────────
_cache = {"time": 0, "data": None}


def register(ctx):
    """插件注册入口"""
    ctx.command("/mem", handle_mem, priority=5,
                description="查看各插件内存占用统计")

    # 定时刷新缓存（每 5 分钟；统计较重：gc.collect + 递归遍历对象图，避免挤占事件循环）
    ctx.task("*/5 * * * *", task_refresh, description="刷新插件内存统计缓存")

    # 仪表盘卡片：进程内存概览
    ctx.dashboard_card("进程内存", _card_mem_overview, icon="📊", priority=15)
    # 仪表盘卡片：插件内存明细（detail 字段供 WebUI 页面渲染使用）
    ctx.dashboard_card("插件内存 TOP", _card_mem_detail, icon="🧩", priority=16)

    # 注册 WebUI 独立页面（plugins/plugin_memmon/web/index.html）
    ctx.webui("内存监控", "index.html", icon="📊", order=10)

    # 内存诊断命令（原 memdiag 插件功能，合并于此）
    ctx.command("/memdiag", handle_diag, priority=999,
                description="重新采集内存诊断报告")

    ctx.log("内存监控插件已注册")


# ══════════════════════════════════════════════════════════
#  内存估算核心
# ══════════════════════════════════════════════════════════

def _estimate(obj, seen=None, depth=0):
    """
    递归估算对象占用内存字节数，返回 (size_bytes, obj_count)

    :param obj:   待估算对象
    :param seen:  已访问对象 id 集合（id 去重，防止循环引用重复计数）
    :param depth: 当前递归深度
    """
    if seen is None:
        seen = set()

    oid = id(obj)
    # 已访问 / 超深度 / 超上限 → 不再深入
    if oid in seen or depth > _MAX_DEPTH or len(seen) > _MAX_OBJECTS:
        return 0, 0
    seen.add(oid)

    try:
        size = sys.getsizeof(obj)
    except Exception:
        size = 0
    count = 1

    try:
        if isinstance(obj, dict):
            for k, v in obj.items():
                sk, ck = _estimate(k, seen, depth + 1)
                sv, cv = _estimate(v, seen, depth + 1)
                size += sk + sv
                count += ck + cv
        elif isinstance(obj, (list, tuple, set, frozenset)):
            for item in obj:
                s, c = _estimate(item, seen, depth + 1)
                size += s
                count += c
        elif isinstance(obj, (str, bytes, int, float, bool, type(None))):
            pass  # 基本类型大小已包含在 sys.getsizeof 中
        else:
            # 其他对象：遍历实例 __dict__ 或 __slots__ 中的引用
            try:
                if hasattr(obj, "__dict__"):
                    s, c = _estimate(obj.__dict__, seen, depth + 1)
                    size += s
                    count += c
                elif hasattr(obj, "__slots__"):
                    for slot in getattr(obj, "__slots__", []):
                        try:
                            s, c = _estimate(getattr(obj, slot), seen, depth + 1)
                            size += s
                            count += c
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        pass

    return size, count


def _estimate_module(module):
    """
    估算一个插件模块的（内存字节, 对象数）
    通过递归 module.__dict__ 的对象图实现
    """
    if module is None:
        return 0, 0
    try:
        return _estimate(module)
    except Exception:
        return 0, 0


def _get_process_mem_mb():
    """获取当前框架进程的物理内存占用（MB）"""
    try:
        return round(psutil.Process().memory_info().rss / 1024 / 1024, 2)
    except Exception:
        return 0.0


# ══════════════════════════════════════════════════════════
#  数据收集
# ══════════════════════════════════════════════════════════

def _collect_stats(ctx):
    """
    收集所有已加载插件的内存统计
    返回 dict：{total_mem_bytes, total_mem_mb, process_mem_mb,
                plugin_count, plugins: [...], updated}
    """
    loader = ctx._framework.plugin_loader
    try:
        loaded = loader.get_loaded_plugins()  # {name: {meta, priority, yaml}}
    except Exception:
        loaded = {}

    stats = []
    total_bytes = 0

    for name in sorted(loaded.keys()):
        module = None
        try:
            module = loader.get_plugin_module(name)
        except Exception:
            pass

        mem_bytes = 0
        obj_count = 0
        if module is not None:
            mem_bytes, obj_count = _estimate_module(module)
            # 统计模块内定义的函数数量（作为插件"服务/接口"规模参考）
            try:
                func_count = sum(1 for v in vars(module).values()
                                 if inspect.isfunction(v))
            except Exception:
                func_count = 0
        else:
            func_count = 0

        meta = (loaded.get(name) or {}).get("meta", {}) or {}
        stats.append({
            "plugin": name,
            "name": meta.get("name", name),
            "version": meta.get("version", "?"),
            "desc": meta.get("desc", ""),
            "mem_bytes": mem_bytes,
            "mem_mb": round(mem_bytes / 1024 / 1024, 2),
            "obj_count": obj_count,
            "func_count": func_count,
        })
        total_bytes += mem_bytes

    # 按内存从大到小排序
    stats.sort(key=lambda x: x["mem_bytes"], reverse=True)

    return {
        "total_mem_bytes": total_bytes,
        "total_mem_mb": round(total_bytes / 1024 / 1024, 2),
        "process_mem_mb": _get_process_mem_mb(),
        "plugin_count": len(stats),
        "plugins": stats,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _get_cache_ttl(ctx):
    """读取配置面板中的刷新间隔（秒），失败用默认值"""
    try:
        val = ctx.get_config("refresh_interval", _DEFAULT_TTL)
        val = int(val)
        return val if val > 0 else _DEFAULT_TTL
    except Exception:
        return _DEFAULT_TTL


def _get_stats(ctx, force=False):
    """读取统计结果，缓存过期或强制时重新收集"""
    global _cache
    now = time.time()
    ttl = _get_cache_ttl(ctx)
    if (force or not _cache["data"] or (now - _cache["time"]) > ttl):
        try:
            gc.collect()  # 先回收垃圾，让估算更接近实际
            _cache["data"] = _collect_stats(ctx)
            _cache["time"] = now
        except Exception as e:
            ctx.log(f"收集插件内存统计失败: {e}", level="error")
            if not _cache["data"]:
                _cache["data"] = {
                    "total_mem_bytes": 0, "total_mem_mb": 0,
                    "process_mem_mb": 0, "plugin_count": 0,
                    "plugins": [], "updated": "",
                }
    return _cache["data"]


def _get_stats_cached():
    """仪表盘卡片专用：只读缓存，过期也不现场统计（避免在 Web 线程卡死）。

    统计较重（gc.collect + 递归遍历对象图），只允许在 /mem 命令和后台定时
    任务中刷新；卡片展示 1-5 分钟前的数据完全可接受。
    """
    if _cache["data"] is not None:
        return _cache["data"]
    # 完全没有缓存时给一个占位（后台定时任务稍后填充）
    return {
        "total_mem_bytes": 0, "total_mem_mb": 0,
        "process_mem_mb": 0, "plugin_count": 0,
        "plugins": [], "updated": "",
    }


def task_refresh():
    """定时任务：刷新统计缓存（每 1 分钟）"""
    try:
        _get_stats(ctx, force=True)
        ctx.log("插件内存统计缓存已刷新")
    except Exception as e:
        ctx.log(f"刷新插件内存统计失败: {e}", level="error")


# ══════════════════════════════════════════════════════════
#  命令处理
# ══════════════════════════════════════════════════════════

def handle_mem(event, match):
    """/mem 查看各插件内存占用"""
    data = _get_stats(ctx, force=True)
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


# ══════════════════════════════════════════════════════════
#  仪表盘卡片
# ══════════════════════════════════════════════════════════

def _card_mem_overview():
    """仪表盘卡片：框架进程内存概览（只读缓存，不现场统计）"""
    data = _get_stats_cached()
    return {
        "value": f"{data.get('process_mem_mb', 0):.1f} MB",
        "label": f"进程内存 · {data.get('plugin_count', 0)} 个插件 / 估算 {data.get('total_mem_mb', 0)} MB",
        "icon": "📊",
    }


def _card_mem_detail():
    """
    仪表盘卡片：插件内存 TOP
    detail 字段携带完整统计，供 WebUI 页面（web/index.html）渲染
    只读缓存，不现场统计（防 Web 线程卡死）
    """
    data = _get_stats_cached()
    plugins = data.get("plugins", [])
    top = plugins[:3]
    if top:
        value = "  ".join(f"{p['name']} {p['mem_mb']}MB" for p in top)
    else:
        value = "暂无数据"
    return {
        "value": value,
        "label": f"插件估算内存合计 {data.get('total_mem_mb', 0)} MB",
        "icon": "🧩",
        "detail": data,  # WebUI 页面数据源
    }


def on_unload():
    """插件卸载时的清理"""
    global _cache
    _cache = {"time": 0, "data": None}
    try:
        ctx.log("内存监控插件已卸载")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════
#  进程内存诊断（原 memdiag 插件功能，合并于此）
# ══════════════════════════════════════════════════════════

# 需要重点检查的重型库
_HEAVY_LIBS = [
    "faiss", "numpy", "PIL", "cv2", "torch", "tensorflow", "sklearn",
    "scipy", "pandas", "jieba", "networkx", "matplotlib", "aiohttp",
    "httpx", "playwright", "selenium", "bs4", "onnxruntime",
    "asyncio_dgram", "mcstatus", "pydantic", "flask", "waitress",
]


def handle_diag(event, match):
    """命令：重新采集内存诊断报告（含与上次快照的增量对比，定位增长源）"""
    try:
        report = _collect_diag(ctx)
        dat_dir = ctx.get_data_dir()
        report_path = os.path.join(dat_dir, "report.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)

        # 与上次快照对比，找出增长最多的对象类型（内存"堆叠"定位）
        snap_path = os.path.join(dat_dir, "snapshot.json")
        prev = {}
        if os.path.isfile(snap_path):
            try:
                with open(snap_path, "r", encoding="utf-8") as f:
                    prev = json.load(f)
            except Exception:
                prev = {}
        growth = _diff_gc_types(prev.get("gc_type_top"), report.get("gc_type_top", []))
        # 写新快照
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump({"time": report.get("time"), "gc_type_top": report.get("gc_type_top", [])},
                      f, ensure_ascii=False)

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


def _diff_gc_types(prev_list, cur_list):
    """
    对比两次 gc 对象类型统计，返回增长最多的类型列表
    [(type, delta_mb, cur_mb), ...]，按增量降序 Top 8
    """
    if not prev_list or not cur_list:
        return []
    prev_map = {x.get("type"): x.get("size_mb", 0) for x in prev_list}
    deltas = []
    for x in cur_list:
        t = x.get("type")
        cur_mb = x.get("size_mb", 0)
        delta = cur_mb - prev_map.get(t, 0)
        if delta > 0.01:  # 只保留 >10KB 的增长
            deltas.append((t, delta, cur_mb))
    deltas.sort(key=lambda d: d[1], reverse=True)
    return deltas[:8]


def _collect_diag(ctx):
    """采集进程内存构成报告（RSS/USS/PSS、内存映射、重型库、gc 对象）"""
    proc = psutil.Process()
    mif = proc.memory_full_info()
    rss = mif.rss
    uss = getattr(mif, "uss", 0) or 0
    pss = getattr(mif, "pss", 0) or 0

    # 1. 内存映射 Top（Linux /proc/self/smaps）
    top_maps = []
    try:
        maps = proc.memory_maps()
        grouped = defaultdict(lambda: {"rss": 0, "count": 0})
        for m in maps:
            key = m.path if m.path else "(匿名堆/栈)"
            grouped[key]["rss"] += m.rss
            grouped[key]["count"] += 1
        for path, info in grouped.items():
            top_maps.append({
                "path": path,
                "rss_mb": round(info["rss"] / 1024 / 1024, 2),
                "maps": info["count"],
            })
        top_maps.sort(key=lambda x: x["rss_mb"], reverse=True)
    except Exception as e:
        top_maps = [{"error": str(e)}]

    # 2. 重型库加载检查
    libs = {}
    for lib in _HEAVY_LIBS:
        mod = sys.modules.get(lib)
        if mod is not None:
            try:
                size = sys.getsizeof(mod)
            except Exception:
                size = 0
            libs[lib] = {"loaded": True, "module_obj_size": size}
        else:
            libs[lib] = {"loaded": False}

    # 3. sys.modules 顶层包统计
    top_pkgs = Counter()
    for name in sys.modules:
        top_pkgs[name.split(".")[0]] += 1
    top_modules = top_pkgs.most_common(40)

    # 4. gc 对象类型统计（估算占用）
    gc.collect()
    type_count = Counter()
    type_size = defaultdict(int)
    for obj in gc.get_objects():
        t = type(obj)
        mod = getattr(t, "__module__", "") or ""
        if not isinstance(mod, str):
            mod = str(mod)
        tn = mod + "." + getattr(t, "__name__", "?")
        type_count[tn] += 1
        try:
            type_size[tn] += sys.getsizeof(obj)
        except Exception:
            pass
    gc_types = []
    for tn, cnt in type_count.most_common(60):
        gc_types.append({
            "type": tn, "count": cnt,
            "size_bytes": type_size[tn],
            "size_mb": round(type_size[tn] / 1024 / 1024, 3),
        })
    gc_types.sort(key=lambda x: x["size_bytes"], reverse=True)

    # 5. 已加载插件
    plugins = []
    try:
        loaded = ctx._framework.plugin_loader.get_loaded_plugins()
        plugins = sorted(loaded.keys())
    except Exception:
        pass

    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "rss_mb": round(rss / 1024 / 1024, 2),
        "uss_mb": round(uss / 1024 / 1024, 2),
        "pss_mb": round(pss / 1024 / 1024, 2),
        "loaded_plugins": plugins,
        "module_count": len(sys.modules),
        "heavy_libs": libs,
        "top_modules": top_modules,
        "top_maps": top_maps[:40],
        "gc_type_top": gc_types[:30],
    }

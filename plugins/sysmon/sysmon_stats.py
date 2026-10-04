# -*- coding: utf-8 -*-
"""运行监控 - 数据采集模块

职责：
- 插件内存估算：遍历各插件模块全局变量的对象图，递归 sys.getsizeof() 累加、
  id() 去重防循环引用，得到估算内存与对象数（Python 无法精确测量单模块内存，
  估算值仅供参考）。
- 进程内存诊断：RSS/USS/PSS、内存映射 Top、重型库加载检查、gc 对象类型统计，
  并支持与上次快照对比增量，定位内存增长来源。
- 系统信息：CPU / 内存 / 磁盘 / 主机信息（psutil）。
- 框架信息：运行时长、在线 bot、插件数、命令数、用户与群数量。

缓存约定：
重统计（gc.collect + 递归遍历对象图）只允许由 /mem、/memdiag 命令与后台定时
任务触发；仪表盘卡片走只读缓存（get_mem_stats_cached），避免卡住 Web 线程。
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

# 插件（套件）本次加载时间，作为 /uptime 的计时起点
_plugin_start_time = time.time()

# ── 内存估算参数 ──────────────────────────────────────────
_DEFAULT_TTL = 60        # 统计缓存有效期（秒），可被配置项 refresh_interval 覆盖
_MAX_DEPTH = 6           # 递归最大深度（防止过深递归导致卡死）
_MAX_OBJECTS = 500000    # 单次统计最大对象数（防止遍历超大对象图导致卡死）

# ── 缓存 ──────────────────────────────────────────────────
_mem_cache = {"time": 0, "data": None}    # 插件内存统计缓存
_sys_cache = {"time": 0, "data": None}    # 系统信息缓存

_EMPTY_MEM_STATS = {
    "total_mem_bytes": 0, "total_mem_mb": 0,
    "process_mem_mb": 0, "plugin_count": 0,
    "plugins": [], "updated": "",
}

# 需要重点检查的重型库（诊断报告用）
HEAVY_LIBS = [
    "faiss", "numpy", "PIL", "cv2", "torch", "tensorflow", "sklearn",
    "scipy", "pandas", "jieba", "networkx", "matplotlib", "aiohttp",
    "httpx", "playwright", "selenium", "bs4", "onnxruntime",
    "asyncio_dgram", "mcstatus", "pydantic", "flask", "waitress",
]


# ══════════════════════════════════════════════════════════
#  插件内存估算
# ══════════════════════════════════════════════════════════

def _estimate(obj, seen=None, depth=0):
    """递归估算对象占用内存字节数，返回 (size_bytes, obj_count)"""
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


def estimate_module(module):
    """估算一个插件模块的（内存字节, 对象数），失败返回 (0, 0)"""
    if module is None:
        return 0, 0
    try:
        return _estimate(module)
    except Exception:
        return 0, 0


def get_process_mem_mb():
    """获取当前框架进程的物理内存占用（MB）"""
    try:
        return round(psutil.Process().memory_info().rss / 1024 / 1024, 2)
    except Exception:
        return 0.0


def collect_plugin_mem(ctx):
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

    result = []
    total_bytes = 0

    for name in sorted(loaded.keys()):
        module = None
        try:
            module = loader.get_plugin_module(name)
        except Exception:
            pass

        mem_bytes = 0
        obj_count = 0
        func_count = 0
        if module is not None:
            mem_bytes, obj_count = estimate_module(module)
            # 统计模块内定义的函数数量（作为插件"服务/接口"规模参考）
            try:
                func_count = sum(1 for v in vars(module).values()
                                 if inspect.isfunction(v))
            except Exception:
                func_count = 0

        meta = (loaded.get(name) or {}).get("meta", {}) or {}
        result.append({
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
    result.sort(key=lambda x: x["mem_bytes"], reverse=True)

    return {
        "total_mem_bytes": total_bytes,
        "total_mem_mb": round(total_bytes / 1024 / 1024, 2),
        "process_mem_mb": get_process_mem_mb(),
        "plugin_count": len(result),
        "plugins": result,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _get_refresh_interval(ctx):
    """读取配置的缓存有效期（秒），非法时用默认值"""
    try:
        val = int(ctx.get_config("refresh_interval", _DEFAULT_TTL))
        return val if val > 0 else _DEFAULT_TTL
    except Exception:
        return _DEFAULT_TTL


def get_mem_stats(ctx, force=False):
    """读取插件内存统计，缓存过期或强制时重新收集"""
    now = time.time()
    ttl = _get_refresh_interval(ctx)
    if (force or not _mem_cache["data"] or (now - _mem_cache["time"]) > ttl):
        try:
            gc.collect()  # 先回收垃圾，让估算更接近实际
            _mem_cache["data"] = collect_plugin_mem(ctx)
            _mem_cache["time"] = now
        except Exception as e:
            ctx.log(f"收集插件内存统计失败: {e}", level="error")
            if not _mem_cache["data"]:
                _mem_cache["data"] = dict(_EMPTY_MEM_STATS)
    return _mem_cache["data"]


def get_mem_stats_cached():
    """仪表盘卡片 / WebUI 专用：只读缓存，过期也不现场统计（避免在 Web 线程卡死）"""
    if _mem_cache["data"] is not None:
        return _mem_cache["data"]
    return dict(_EMPTY_MEM_STATS)


def refresh_mem_stats(ctx):
    """定时任务：强制刷新插件内存统计缓存"""
    try:
        get_mem_stats(ctx, force=True)
        ctx.log("插件内存统计缓存已刷新")
    except Exception as e:
        ctx.log(f"刷新插件内存统计失败: {e}", level="error")


def clear_mem_cache():
    """清空插件内存统计缓存（插件卸载时调用）"""
    _mem_cache["time"] = 0
    _mem_cache["data"] = None


# ══════════════════════════════════════════════════════════
#  进程内存诊断
# ══════════════════════════════════════════════════════════

def diff_gc_types(prev_list, cur_list):
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


def collect_diag_report(ctx):
    """采集进程内存构成报告（RSS/USS/PSS、内存映射、重型库、gc 对象类型）"""
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
    for lib in HEAVY_LIBS:
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


def run_mem_diag(ctx):
    """
    执行一次内存诊断：采集报告写入插件数据目录，并与上次快照对比增长。
    返回 (report, growth)，growth 为 [(type, delta_mb, cur_mb), ...]。
    """
    report = collect_diag_report(ctx)
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
    growth = diff_gc_types(prev.get("gc_type_top"), report.get("gc_type_top", []))
    # 写新快照
    with open(snap_path, "w", encoding="utf-8") as f:
        json.dump({"time": report.get("time"), "gc_type_top": report.get("gc_type_top", [])},
                  f, ensure_ascii=False)
    return report, growth


# ══════════════════════════════════════════════════════════
#  系统信息 / 框架信息
# ══════════════════════════════════════════════════════════

def format_uptime(seconds):
    """把秒数格式化为「x天x小时x分钟x秒」"""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    parts = []
    if days > 0:
        parts.append(f"{days}天")
    if hours > 0:
        parts.append(f"{hours}小时")
    if mins > 0:
        parts.append(f"{mins}分钟")
    parts.append(f"{secs}秒")
    return "".join(parts)


def get_system_info(ctx):
    """收集系统信息（带缓存，时长由配置项 status_interval 控制）"""
    now = time.time()
    try:
        interval = float(ctx.get_config("status_interval", 30) or 30)
    except Exception:
        interval = 30
    if _sys_cache["data"] and (now - _sys_cache["time"]) < interval:
        return _sys_cache["data"]

    try:
        import platform
        import socket
        vm = psutil.virtual_memory()
        cpu_percent = psutil.cpu_percent(interval=0.5)
        disk = psutil.disk_usage('/')
        info = {
            'cpu_percent': cpu_percent,
            'cpu_count': psutil.cpu_count(logical=True),
            'mem_total': vm.total / 1024 / 1024 / 1024,
            'mem_used': vm.used / 1024 / 1024 / 1024,
            'mem_percent': vm.percent,
            'disk_total': disk.total / 1024 / 1024 / 1024,
            'disk_used': disk.used / 1024 / 1024 / 1024,
            'disk_percent': disk.percent,
            'platform': platform.platform(),
            'python': platform.python_version(),
            'hostname': socket.gethostname(),
            'boot_time': format_uptime(now - psutil.boot_time()),
        }
        _sys_cache["data"] = info
        _sys_cache["time"] = now
        return info
    except Exception as e:
        traceback.print_exc()
        return {'error': str(e)}


def clear_system_cache():
    """清空系统信息缓存（定时任务调用，保证数据周期性重采）"""
    _sys_cache["time"] = 0
    _sys_cache["data"] = None


def get_framework_info(ctx):
    """收集框架运行信息（运行时长 / 端口 / 在线 bot / 插件与命令统计等）"""
    try:
        framework = ctx._framework
        bots = framework.ws_server.get_connected_bots()
        loaded_plugins = framework.plugin_loader.get_loaded_plugins()

        db = ctx._db
        cmd_count = db.query_one("SELECT COUNT(*) as cnt FROM commands")['cnt']
        dyn_count = db.query_one("SELECT COUNT(*) as cnt FROM dynamic_commands WHERE is_active=1")['cnt']
        user_count = db.query_one("SELECT COUNT(*) as cnt FROM users")['cnt']
        group_count = db.query_one("SELECT COUNT(*) as cnt FROM groups_info WHERE is_active=1")['cnt']

        return {
            'uptime': format_uptime(time.time() - _plugin_start_time),
            'ws_port': framework.config.get('onebot', {}).get('listen_port', 6830),
            'web_port': framework.config.get('web', {}).get('port', 8080),
            'bot_count': len(bots),
            'bots': bots,
            'plugin_count': len(loaded_plugins),
            'plugins': list(loaded_plugins.keys()),
            'cmd_count': cmd_count,
            'dyn_count': dyn_count,
            'user_count': user_count,
            'group_count': group_count,
        }
    except Exception as e:
        return {'error': str(e)}

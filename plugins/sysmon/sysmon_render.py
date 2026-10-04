# -*- coding: utf-8 -*-
"""运行监控 - 状态卡片渲染模块

通过共享图片渲染引擎绘制「框架运行状态」卡片图（原生 Rust 扩展优先，
自动回退 PIL）。渲染引擎以模块名 plugin_image_renderer 挂在 sys.modules
上，本模块只经 sys.modules 桥接其公开绘制接口，不读写任何其他插件目录。

字体解析完全自给，按以下顺序取第一个命中的 .ttf/.otf/.ttc：
  1. 渲染引擎自行解析出的字体路径（引擎对自己目录的字体自管理）；
  2. 本插件数据目录（ctx.get_data_dir()，可手动放入字体文件）；
  3. 系统字体目录（Windows / Linux / macOS 常见中文字体）。
全部落空则不出图，由调用方回退纯文本输出。
"""
import os
import sys
import tempfile

# 结果缓存：字体扫描（尤其系统目录递归）只在首次进行
_font_path_cache = None

# 本插件数据目录下按顺序尝试的字体文件名（用户可自行放置）
_DATA_DIR_FONT_NAMES = ["font.ttf", "font.otf", "font.ttc"]

# Windows 系统字体目录下常见中文字体（按优先级）
_WIN_FONT_NAMES = [
    "msyh.ttc", "msyh.ttf", "msyhbd.ttc",          # 微软雅黑
    "simhei.ttf", "simsun.ttc",                     # 黑体 / 宋体
    "Deng.ttf", "DengXian.ttf", "Dengb.ttf",        # 等线
]

# 非 Windows 平台递归扫描时，文件名包含这些关键词即视为中文字体
_UNIX_FONT_KEYWORDS = [
    "cjk", "noto", "wqy", "zenhei", "uming", "ukai", "droid",
    "pingfang", "stheiti", "hiragino", "sourcehan", "sarasa",
]


def _is_font_file(path):
    """判断路径是否为受支持的字体文件"""
    return path.lower().endswith((".ttf", ".otf", ".ttc")) and os.path.isfile(path)


def _resolve_from_renderer():
    """从共享渲染引擎取它自己解析好的字体路径；引擎未加载或未提供时返回 None"""
    mod = sys.modules.get("plugin_image_renderer")
    if mod is None or not hasattr(mod, "_find_font_path"):
        return None
    try:
        path = mod._find_font_path()
    except Exception:
        return None
    if path and _is_font_file(path):
        return path
    return None


def _resolve_from_data_dir(ctx):
    """从插件数据目录（ctx.get_data_dir()）找字体"""
    try:
        dat_dir = ctx.get_data_dir()
    except Exception:
        return None
    # 先试固定文件名，再兜底任意字体文件
    for name in _DATA_DIR_FONT_NAMES:
        p = os.path.join(dat_dir, name)
        if _is_font_file(p):
            return p
    try:
        for name in sorted(os.listdir(dat_dir)):
            p = os.path.join(dat_dir, name)
            if _is_font_file(p):
                return p
    except Exception:
        pass
    return None


def _resolve_from_system():
    """扫描系统字体目录，找一个可用的中文字体"""
    if sys.platform.startswith("win"):
        windir = os.environ.get("WINDIR", r"C:\Windows")
        font_dir = os.path.join(windir, "Fonts")
        for name in _WIN_FONT_NAMES:
            p = os.path.join(font_dir, name)
            if _is_font_file(p):
                return p
        # 候选名单都没有时，兜底扫一遍目录里的 ttf/ttc
        try:
            for name in sorted(os.listdir(font_dir)):
                p = os.path.join(font_dir, name)
                if p.lower().endswith((".ttf", ".ttc")) and os.path.isfile(p):
                    return p
        except Exception:
            pass
        return None

    # Linux / macOS：递归扫描常见字体目录，按关键词命中中文字体
    roots = [
        "/usr/share/fonts", "/usr/local/share/fonts",
        "/System/Library/Fonts", "/Library/Fonts",
    ]
    scanned = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                scanned += 1
                if scanned > 4000:  # 扫描上限，防异常超大目录拖慢
                    return None
                low = fn.lower()
                if not low.endswith((".ttf", ".otf", ".ttc")):
                    continue
                if any(k in low for k in _UNIX_FONT_KEYWORDS):
                    return os.path.join(dirpath, fn)
    return None


def resolve_font_path(ctx):
    """解析可用于绘制的字体路径；找不到返回 None（调用方回退纯文本）"""
    global _font_path_cache
    if _font_path_cache is not None:
        # 缓存的字体文件可能被删除，使用前复核
        if os.path.isfile(_font_path_cache):
            return _font_path_cache
        _font_path_cache = None
    for resolver in (_resolve_from_renderer, _resolve_from_data_dir, _resolve_from_system):
        try:
            path = resolver(ctx) if resolver is not _resolve_from_system else resolver()
        except Exception:
            path = None
        if path:
            _font_path_cache = path
            return path
    return None


def _get_renderer():
    """取共享渲染引擎模块；未加载或缺少画布接口时返回 None"""
    mod = sys.modules.get("plugin_image_renderer")
    if mod is None or not hasattr(mod, "_get_native_or_pil_canvas"):
        return None
    return mod


def render_status_card(ctx, fw, proc_mem, sys_info, show_cpu):
    """绘制框架运行状态卡片图；渲染引擎缺失或渲染失败时返回 None"""
    mod = _get_renderer()
    if mod is None:
        return None
    font_path = resolve_font_path(ctx)
    if not font_path:
        return None
    try:
        W = 560
        row_h = 30
        rows = [
            ("运行时间", fw.get('uptime', '-')),
            ("进程内存", f"{proc_mem:.1f} MB"),
        ]
        if show_cpu and 'error' not in (sys_info or {}):
            rows.append(("CPU 使用率", f"{sys_info['cpu_percent']}% ({sys_info['cpu_count']}核)"))
        rows += [
            ("OneBot 客户端", f"{fw.get('bot_count', 0)} 个在线"),
            ("已加载插件", f"{fw.get('plugin_count', 0)} 个"),
            ("注册命令", f"{fw.get('cmd_count', 0)} 条"),
            ("动态命令", f"{fw.get('dyn_count', 0)} 条"),
            ("用户数", f"{fw.get('user_count', 0)}"),
            ("活跃群", f"{fw.get('group_count', 0)}"),
            ("WebSocket", f":{fw.get('ws_port', '-')}"),
            ("Web UI", f":{fw.get('web_port', '-')}"),
        ]
        H = 44 + len(rows) * row_h + 26
        canvas = mod._get_native_or_pil_canvas(W, H, None, font_path)
        canvas.rect(0, 0, W, H, radius=0, fill="#0f1420")
        canvas.rect(0, 0, 6, H, radius=0, fill="#4a90d9")
        canvas.text(24, 18, "📊 框架运行状态", font_size=22, color="#FFFFFF")
        y = 58
        right_margin = W - 28   # 数值右对齐的右边距
        label_right = 150       # 标签允许的最大右侧（预留给数值）
        for label, value in rows:
            label = str(label)
            value = str(value)
            # 标签（左对齐，超宽截断）
            try:
                lw, _ = canvas.text_metrics(label, 16)
            except Exception:
                lw = len(label) * 8
            if lw > label_right - 28:
                label = label[:10] + "…"
            canvas.text(28, y, label, font_size=16, color="#9fb3cc")
            # 数值：右对齐到 right_margin，超宽逐字符截断
            try:
                vw, _ = canvas.text_metrics(value, 16)
            except Exception:
                vw = len(value) * 8
            max_vw = right_margin - label_right
            if vw > max_vw:
                while len(value) > 1 and vw > max_vw:
                    value = value[:-1]
                    try:
                        vw, _ = canvas.text_metrics(value + "…", 16)
                    except Exception:
                        vw = len(value) * 8
                value = value + "…"
            canvas.text(right_margin - vw, y, value, font_size=16, color="#7fd8a8")
            y += row_h
        return canvas.to_png()
    except Exception as e:
        try:
            ctx.log(f"状态图渲染失败: {e}", level="error")
        except Exception:
            pass
        return None


def send_png(ctx, event, png_bytes):
    """把 PNG 字节写入临时文件并以图片消息发送，发送后清理临时文件"""
    img_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            img_path = tmp.name
            tmp.write(png_bytes)
        path_str = img_path.replace("\\", "/")  # Windows 路径反斜杠需转正斜杠
        ctx.api("send_msg",
                user_id=event.user_id,
                group_id=event.group_id,
                message=f"[CQ:image,file=file:///{path_str}]")
        return True
    except Exception as e:
        try:
            ctx.log(f"发送状态图片失败: {e}", level="error")
        except Exception:
            pass
        return False
    finally:
        if img_path:
            try:
                os.unlink(img_path)
            except Exception:
                pass

# -*- coding: utf-8 -*-
"""
DBCJ MC 状态监控插件
====================
专门为 dbcj.top 服务器开发的状态探针 + 心电图式渲染插件。

功能：
- 每分钟检测一次 Minecraft 服务器（mcstatus 自动解析 SRV 记录，端口不限于 25565）
- 将检测结果（在线状态、玩家人数、最大人数、延迟、版本、MOTD）写入框架数据库
- 数据库最多保留 max_rows(默认10000) 条记录，超出自动清理最旧数据
- 发送 /dbcj 时渲染"心电图"风格状态图（玩家人数 + 延迟 画在同一张图，不同颜色线条）并发送图片

命令：
  /dbcj /mc /服务器            渲染心电图状态图并发送图片
  /dbcj 状态 /dbcj status      查看文字版当前状态
  /dbcj 数据 [N] /dbcj data 30 查看最近 N 条原始记录
  /dbcj 设置 /dbcj config      查看当前生效配置
  /dbcj 设置 <键> <值>         修改配置（host/port/timeout/max_rows/points，存数据库持久化）
  /dbcj 重置                   把配置恢复为默认值
  /dbcj 测试 /dbcj now         立即探测一次并返回详细结果（含解析出的真实地址）
  /dbcj 帮助 /dbcj help        查看使用帮助

配置项（读取优先级：数据库运行期设置 > ctx.get_config > 内置默认值）：
  server_host    服务器地址，默认 dbcj.top
  server_port    服务器端口，默认 25565（SRV 记录会覆盖）
  probe_timeout  探测超时（秒），默认 10
  max_rows       数据库最大保留行数，默认 10000
  chart_points   图表展示最近多少条记录，默认 120
  probe_cron     探测定时 cron，默认 * * * * *（每分钟）
"""
import base64
import io
import math
import os
import re
import threading
import time
from datetime import datetime

__plugin_meta__ = {
    "name": "DBCJ MC 状态监控",
    "version": "1.2.1",
    "author": "ZGRIC",
    "desc": "dbcj.top 服务器状态探针：每分钟检测记录，/dbcj 渲染心电图式状态图（人数+延迟同图双线），支持运行期改配置",
    "priority": 50,
}

# ctx 由框架注入为模块全局变量（在 register(ctx) 调用前）
ctx = None

# 防止并发探测重入
_probe_lock = threading.Lock()

# 运行期配置覆盖（从数据库加载，优先级最高）
_CONF_OVERRIDES = {}

# ═══════════════════════════ 建表 ═══════════════════════════
# 自动适配 SQLite / MySQL（框架 db.py 会翻译 DDL 与 %s 占位符）
# 注意：MySQL 不允许 TEXT 列带默认值，故用 VARCHAR
_CREATE_DATA_TABLE = """
CREATE TABLE IF NOT EXISTS dbcj_mcstatus (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL DEFAULT 0,
    online INTEGER NOT NULL DEFAULT 0,
    players INTEGER NOT NULL DEFAULT 0,
    max_players INTEGER NOT NULL DEFAULT 0,
    latency REAL NOT NULL DEFAULT -1,
    version VARCHAR(255) DEFAULT '',
    motd VARCHAR(255) DEFAULT ''
)
"""

_CREATE_CONF_TABLE = """
CREATE TABLE IF NOT EXISTS dbcj_mcstatus_conf (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conf_key VARCHAR(64) NOT NULL DEFAULT '',
    conf_value VARCHAR(255) NOT NULL DEFAULT '',
    updated_ts INTEGER NOT NULL DEFAULT 0
)
"""

# ═══════════════════════════ 配色（心电图风格） ═══════════════════════════
_BG = (8, 16, 14)            # 深色背景
_GRID = (22, 48, 38)         # 网格线
_GRID_DIM = (16, 38, 30)     # 弱网格线
_TEXT = (140, 210, 170)      # 主文字（淡绿）
_TEXT_DIM = (78, 130, 108)   # 次要文字
_GREEN = (70, 255, 130)      # 在线 / 标题
_RED = (255, 90, 90)         # 离线
_CYAN = (80, 220, 255)       # 玩家曲线
_ORANGE = (255, 170, 80)     # 延迟曲线
_OFFLINE_BAND = (46, 12, 12) # 离线区间的暗红色带
_YELLOW = (255, 215, 90)     # 延迟数字

# ═══════════════════════════ 字体 ═══════════════════════════
_FONT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fun_score", "resource", "font",
)
_FONT_CACHE = {}


def _chart_font_path() -> str:
    """Canvas 版图表字体路径（优先 fun_score 粗体，回退系统字体）"""
    candidates = [
        os.path.join(_FONT_DIR, "HarmonyOS_Sans_SC_Bold.ttf"),
        os.path.join(_FONT_DIR, "HarmonyOS_Sans_SC_Regular.ttf"),
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


# ═══════════════════════════ 日志 ═══════════════════════════

def _log(msg, level="info"):
    try:
        ctx.log(f"[DBCJ-MCStatus] {msg}", level)
    except Exception:
        pass


# ═══════════════════════════ 配置读取 / 运行期设置 ═══════════════════════════

def _load_conf_overrides():
    """启动时从数据库加载运行期配置覆盖"""
    global _CONF_OVERRIDES
    try:
        rows = ctx.db_query("SELECT conf_key, conf_value FROM dbcj_mcstatus_conf")
        for r in rows:
            _CONF_OVERRIDES[str(r.get("conf_key") or "").strip()] = str(r.get("conf_value") or "")
        # 一次性迁移：早期曾把 server_host 固定为 SRV 解析出的 IP（103.85.86.51:33479），
        # 现 PHP 接口已支持 SRV 自动解析，恢复默认域名 dbcj.top:25565（探测与标题均走域名）
        _reset_legacy_host()
        _log(f"已加载 {len(_CONF_OVERRIDES)} 条运行期配置", "info")
    except Exception as e:
        _log(f"加载运行期配置失败: {e}", "warning")


def _reset_legacy_host():
    """清除指向旧解析 IP 的 host/port 覆盖（幂等，仅当值为已知旧地址时生效）"""
    _LEGACY_HOSTS = {"103.85.86.51", "103.85.86.51:33479"}
    _LEGACY_PORTS = {"33479"}
    removed = []
    hv = (_CONF_OVERRIDES.get("server_host") or "").strip().lower()
    pv = (_CONF_OVERRIDES.get("server_port") or "").strip()
    if hv in _LEGACY_HOSTS or hv == "dbcj.top" and pv in _LEGACY_PORTS:
        for key in ("server_host", "server_port"):
            _CONF_OVERRIDES.pop(key, None)
            removed.append(key)
    elif pv in _LEGACY_PORTS:
        _CONF_OVERRIDES.pop("server_port", None)
        removed.append("server_port")
    if not removed:
        return
    try:
        for key in removed:
            ctx.db_execute("DELETE FROM dbcj_mcstatus_conf WHERE conf_key = %s", (key,))
        _log(f"已重置旧探测地址覆盖 {removed} → 默认 dbcj.top:25565（PHP SRV 自动解析）", "info")
    except Exception as e:
        _log(f"重置旧探测地址覆盖失败: {e}", "warning")


def _get_conf(key, default):
    """读取配置：数据库覆盖 > ctx.get_config > 默认值"""
    v = _CONF_OVERRIDES.get(key)
    if v is not None and str(v).strip() != "":
        return v
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


def _get_str(key, default):
    try:
        return str(_get_conf(key, default)).strip()
    except Exception:
        return default


def _get_int(key, default, min_value=0, max_value=None):
    try:
        v = int(_get_conf(key, default))
        if v < min_value:
            v = min_value
        if max_value is not None and v > max_value:
            v = max_value
        return v
    except (TypeError, ValueError):
        return default
    except Exception:
        return default


def _get_float(key, default, min_value=0, max_value=None):
    try:
        v = float(_get_conf(key, default))
        if v < min_value:
            v = min_value
        if max_value is not None and v > max_value:
            v = max_value
        return v
    except (TypeError, ValueError):
        return default
    except Exception:
        return default


# 中文键名别名 → 内部键
_CONF_ALIAS = {
    "host": "server_host", "ip": "server_host", "地址": "server_host", "服务器": "server_host",
    "域名": "server_host", "server_host": "server_host",
    "port": "server_port", "端口": "server_port", "server_port": "server_port",
    "timeout": "probe_timeout", "超时": "probe_timeout", "probe_timeout": "probe_timeout",
    "max_rows": "max_rows", "maxrows": "max_rows", "保留": "max_rows", "最大行数": "max_rows",
    "points": "chart_points", "点数": "chart_points", "chart_points": "chart_points",
    "chart_points": "chart_points",
}

_CONF_LABELS = {
    "server_host": "服务器地址(host)",
    "server_port": "端口(port)",
    "probe_timeout": "探测超时秒(timeout)",
    "max_rows": "数据库保留行数(max_rows)",
    "chart_points": "图表点数(points)",
}


def _set_conf(key, value):
    """设置运行期配置并持久化到数据库"""
    global _CONF_OVERRIDES
    key = str(key).strip()
    value = str(value).strip()
    if not key or not value:
        return False, "键和值都不能为空"
    if key in ("server_host",):
        pass
    elif key in ("server_port", "probe_timeout", "max_rows", "chart_points"):
        try:
            int(value)
        except (TypeError, ValueError):
            try:
                float(value)
            except (TypeError, ValueError):
                return False, f"「{_CONF_LABELS.get(key, key)}」必须是数字"
    else:
        return False, f"未知配置键：{key}（可用 host/port/timeout/max_rows/points）"
    _CONF_OVERRIDES[key] = value
    now = int(time.time())
    try:
        rows = ctx.db_query("SELECT id FROM dbcj_mcstatus_conf WHERE conf_key = %s", (key,))
        if rows:
            ctx.db_execute(
                "UPDATE dbcj_mcstatus_conf SET conf_value = %s, updated_ts = %s WHERE conf_key = %s",
                (value, now, key),
            )
        else:
            ctx.db_execute(
                "INSERT INTO dbcj_mcstatus_conf (conf_key, conf_value, updated_ts) VALUES (%s, %s, %s)",
                (key, value, now),
            )
    except Exception as e:
        return False, f"配置已生效但写入数据库失败: {e}"
    return True, f"已设置 {_CONF_LABELS.get(key, key)} = {value}"


def _reset_conf():
    """清除全部运行期配置覆盖"""
    global _CONF_OVERRIDES
    _CONF_OVERRIDES = {}
    try:
        ctx.db_execute("DELETE FROM dbcj_mcstatus_conf")
        return "已重置全部配置为默认值"
    except Exception as e:
        return f"运行期配置已清空，但数据库清理失败: {e}"


def _current_conf_text():
    lines = ["当前生效配置："]
    host = _get_str("server_host", "dbcj.top")
    port = _get_int("server_port", 25565, 1, 65535)
    timeout = _get_float("probe_timeout", 10, 1, 60)
    max_rows = _get_int("max_rows", 10000, 100)
    points = _get_int("chart_points", 120, 10, 500)
    lines.append(f"· 服务器：{host}:{port}（SRV 自动解析）")
    lines.append(f"· 探测超时：{timeout:.0f} 秒")
    lines.append(f"· 数据库保留：{max_rows} 条")
    lines.append(f"· 图表点数：{points} 条")
    if _CONF_OVERRIDES:
        lines.append("已覆盖项：" + "、".join(f"{k}={v}" for k, v in _CONF_OVERRIDES.items()))
    else:
        lines.append("当前无覆盖项（全部使用默认值）")
    lines.append("修改：/dbcj 设置 <键> <值>；键支持 host/port/timeout/max_rows/points")
    return "\n".join(lines)


# ═══════════════════════════ 探测 ═══════════════════════════

def _clean_str(s, limit=120):
    try:
        s = str(s).strip()
    except Exception:
        return ""
    # 去掉 Minecraft 颜色/格式代码
    s = re.sub(r"§[0-9a-fk-or]", "", s)
    if len(s) > limit:
        s = s[:limit] + "..."
    return s


def _motd_plain(motd):
    if motd is None:
        return ""
    for attr in ("to_plain", "to_legacy", "to_string"):
        try:
            fn = getattr(motd, attr, None)
            if callable(fn):
                return _clean_str(fn())
        except Exception:
            continue
    try:
        return _clean_str(motd)
    except Exception:
        return ""


def _short_err(e):
    """把 SSL 证书等冗长异常压缩成一行，便于日志与回复阅读"""
    msg = str(e) or repr(e)
    first = msg.split("\n")[0].strip()
    return first[:160] if len(first) > 160 else first


def _unverified_opener():
    """构建一个不校验证书的 opener（兼容自签名 HTTPS 网关/302 重定向到 https）"""
    import ssl as _ssl
    import urllib.request as _ureq
    try:
        ctx = _ssl._create_unverified_context()
        return _ureq.build_opener(
            _ureq.HTTPSHandler(context=ctx),
            _ureq.HTTPRedirectHandler(),
        )
    except Exception:
        # 极端情况下 ssl 模块不可用，退回默认 opener
        import urllib.request as _ureq2
        return _ureq2.build_opener()


def _do_probe(host, port, timeout):
    """探测服务器，返回 (status_ns, addr)。

    走内网 PHP API（127.0.0.1:400/mc/dbcj），不再依赖 mcstatus。
    内网网关可能 302 重定向到 https://dbcj.top（自签名证书），
    故使用不校验证书的 SSL 上下文，避免 CERTIFICATE_VERIFY_FAILED。
    返回的 status_ns 兼容 mcstatus 风格访问（.players.online/.latency/.version.name/.motd），
    上层调用方代码无需改动。
    """
    import json as _json
    import urllib.parse as _uparse
    import urllib.request as _ureq
    from types import SimpleNamespace as _NS

    api_base = _get_str("api_base", "http://127.0.0.1:400/mc/dbcj").rstrip("/")
    url = f"{api_base}?host={_uparse.quote(str(host))}&port={int(port)}"
    try:
        req = _ureq.Request(url, headers={"User-Agent": "ZCBOT-DBCJ-MCStatus/1.2.1"})
        with _unverified_opener().open(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"内网 API 请求失败: {_short_err(e)}")
    if not isinstance(data, dict) or not data.get("online"):
        err = (data or {}).get("error") or "服务器离线或不可达"
        raise RuntimeError(err)

    # 适配成 mcstatus 风格对象（玩家/延迟/版本/MOTD）
    status_ns = _NS(
        players=_NS(
            online=int(data.get("players") or 0),
            max=int(data.get("max_players") or 0),
        ),
        latency=float(data.get("latency") or -1),
        version=_NS(name=str(data.get("version") or "")),
        motd=str(data.get("description") or ""),  # 含 § 码的纯文本，_motd_plain 会剥离
        icon=data.get("favicon") or None,
    )
    addr = _Addr(str(data.get("host") or host), int(data.get("port") or port))
    return status_ns, addr


class _Addr:
    """兼容 mcstatus Address 的 str 表现（host:port）"""

    def __init__(self, host, port):
        self.host = host
        self.port = port

    def __str__(self):
        return f"{self.host}:{self.port}"


def probe_task():
    """定时探测任务：探测 + 写库 + 清理旧数据"""
    if not _probe_lock.acquire(blocking=False):
        return  # 上一次探测还没结束，跳过本次
    try:
        host = _get_str("server_host", "dbcj.top")
        port = _get_int("server_port", 25565, 1, 65535)
        timeout = _get_float("probe_timeout", 10, 1, 60)
        online = False
        players = 0
        max_players = 0
        latency = -1.0
        version = ""
        motd = ""
        success = False
        try:
            status, addr = _do_probe(host, port, timeout)
            success = True
            online = True
            try:
                players = int(getattr(status.players, "online", 0) or 0)
                max_players = int(getattr(status.players, "max", 0) or 0)
            except Exception:
                players = 0
                max_players = 0
            try:
                latency = float(getattr(status, "latency", -1) or -1)
            except Exception:
                latency = -1.0
            version = _clean_str(getattr(getattr(status, "version", None), "name", ""))
            motd = _motd_plain(getattr(status, "motd", None))
            addr_txt = ""
            if addr:
                try:
                    addr_txt = f"（解析到 {addr}）"
                except Exception:
                    addr_txt = ""
            _log(f"探测成功: {host} 在线 {players}/{max_players} 延迟{latency:.0f}ms{addr_txt}", "info")
        except Exception as e:
            online = False
            _log(f"探测失败: {host} 离线或不可达 ({e})", "warning")
        _record(online, players, max_players, latency, version, motd)
    finally:
        _probe_lock.release()


def _record(online, players, max_players, latency, version, motd):
    """写入一条记录并清理超出 max_rows 的旧数据"""
    try:
        ctx.db_execute(
            "INSERT INTO dbcj_mcstatus (ts, online, players, max_players, latency, version, motd) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                int(time.time()),
                1 if online else 0,
                int(players or 0),
                int(max_players or 0),
                float(latency if latency is not None and latency >= 0 else -1),
                _clean_str(version),
                _clean_str(motd),
            ),
        )
        max_rows = _get_int("max_rows", 10000, 100)
        cnt_rows = ctx.db_query("SELECT COUNT(*) AS c FROM dbcj_mcstatus")
        total = int(cnt_rows[0]["c"]) if cnt_rows else 0
        if total > max_rows:
            excess = total - max_rows
            ctx.db_execute(
                "DELETE FROM dbcj_mcstatus WHERE id IN "
                "(SELECT id FROM (SELECT id FROM dbcj_mcstatus ORDER BY id ASC LIMIT %s) t)",
                (excess,),
            )
    except Exception as e:
        _log(f"写入记录失败: {e}", "error")


def _last_row():
    rows = ctx.db_query("SELECT * FROM dbcj_mcstatus ORDER BY id DESC LIMIT 1")
    return rows[0] if rows else None


def _recent_rows(n):
    rows = ctx.db_query("SELECT * FROM dbcj_mcstatus ORDER BY id DESC LIMIT %s", (n,))
    rows.reverse()
    return rows


# ═══════════════════════════ 渲染（人数 + 延迟 同图双线） ═══════════════════════════

def _nice_ceil(v):
    """把数值向上取整到 1/2/5×10^n 的漂亮刻度"""
    try:
        v = float(v)
    except Exception:
        v = 1.0
    if v <= 0:
        return 1
    exp = 10 ** math.floor(math.log10(v))
    f = v / exp
    if f <= 1:
        return exp
    if f <= 2:
        return 2 * exp
    if f <= 5:
        return 5 * exp
    return 10 * exp


def _fmt_ts(ts):
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def render_chart_image(rows):
    """渲染心电图式状态图：玩家人数（青）与延迟（橙）画在同一张图，双 Y 轴。

    Canvas 版（image_renderer 原生渲染，无 PIL）。
    返回 base64 字符串（PNG）。rows 需为按时间升序排列的记录列表。
    """
    import sys as _sys
    import base64 as _b64
    mod = _sys.modules.get("plugin_image_renderer")
    if mod is None or not hasattr(mod, "_get_native_or_pil_canvas"):
        raise RuntimeError("image_renderer 未加载，无法渲染图表")

    W, H = 1000, 620
    canvas = mod._get_native_or_pil_canvas(W, H, _BG, _chart_font_path())

    def text(x, y, s, size, color):
        canvas.text(int(x), int(y), str(s), font_size=int(size), color=color)

    def text_right(x_right, y, s, size, color):
        w, _ = canvas.text_metrics(str(s), int(size))
        canvas.text(int(x_right - w), int(y), str(s), font_size=int(size), color=color)

    host = _get_str("server_host", "dbcj.top")

    # ── 头部信息 ──
    title = f"{host.upper()} 状态监控"
    text(30, 20, title, 30, _GREEN)

    last = rows[-1] if rows else None
    online = bool(last and int(last.get("online") or 0))
    status_text = "● 在线" if online else "● 离线"
    status_color = _GREEN if online else _RED
    text_right(W - 30, 24, status_text, 22, status_color)

    if last:
        players = int(last.get("players") or 0)
        max_players = int(last.get("max_players") or 0)
        latency = float(last.get("latency") or -1)
        version = _clean_str(last.get("version") or "", 40)
        motd = _clean_str(last.get("motd") or "", 60)
        lat_txt = f"{latency:.0f}ms" if latency >= 0 else "--"
        text(30, 66, f"玩家  {players}/{max_players}    延迟  {lat_txt}    版本  {version or '未知'}", 17, _TEXT)
        if motd:
            text(30, 94, f"MOTD  {motd}", 17, _TEXT_DIM)
    else:
        text(30, 66, "暂无数据，等待下一次探测...", 17, _TEXT_DIM)

    # ── 图表区域 ──
    cx0, cx1 = 70, W - 44
    cy0, cy1 = 175, 545
    cw, ch = cx1 - cx0, cy1 - cy0

    # 背景网格
    canvas.rect(cx0, cy0, cx1, cy1, radius=0, fill=(10, 22, 18), outline=_GRID, width=1)
    for i in range(5):
        y = int(cy0 + ch * i / 4)
        canvas.line([(cx0, y), (cx1, y)], _GRID_DIM, 1)
    for i in range(1, 8):
        x = int(cx0 + cw * i / 8)
        canvas.line([(x, cy0), (x, cy1)], _GRID_DIM, 1)

    n = len(rows)
    if n > 0:
        # 计算 Y 轴刻度（人数 / 延迟共用一张图、分开左右轴）
        players_max = max(1, _nice_ceil(max(int(r.get("players") or 0) for r in rows)))
        lat_vals = [float(r.get("latency") or -1) for r in rows if (int(r.get("online") or 0) and float(r.get("latency") or -1) >= 0)]
        latency_max = max(50, _nice_ceil(max(lat_vals))) if lat_vals else 50

        # 左轴（玩家）
        text_right(cx0 - 8, cy0 - 6, str(players_max), 12, _TEXT_DIM)
        text_right(cx0 - 8, cy1 - 4, "0", 12, _TEXT_DIM)
        text(cx0 + 2, cy0 - 24, "玩家", 12, _CYAN)
        # 右轴（延迟）
        text(cx1 + 6, cy0 - 6, str(int(latency_max)), 12, _TEXT_DIM)
        text(cx1 + 6, cy1 - 4, "0", 12, _TEXT_DIM)
        text(cx1 - 30, cy0 - 24, "延迟(ms)", 12, _ORANGE)

        # 图例
        text(cx0 + 2, cy0 - 26, "— 玩家", 12, _CYAN)
        text(cx0 + 70, cy0 - 26, "— 延迟(ms)", 12, _ORANGE)

        def xpos(i):
            if n <= 1:
                return cx0
            return int(cx0 + cw * i / (n - 1))

        # 离线区间：暗红色带
        i = 0
        while i < n:
            if not int(rows[i].get("online") or 0):
                j = i
                while j < n and not int(rows[j].get("online") or 0):
                    j += 1
                xa = xpos(i)
                xb = xpos(j - 1) if j - 1 > i else xpos(i)
                canvas.rect(int(xa), cy0, int(xb), cy1, radius=0, fill=_OFFLINE_BAND)
                i = j
            else:
                i += 1

        # 玩家曲线（始终绘制，离线时为 0 底线）
        players_pts = []
        for idx, r in enumerate(rows):
            p = int(r.get("players") or 0)
            py = int(cy1 - (p / players_max) * ch)
            players_pts.append((xpos(idx), py))
        if len(players_pts) >= 2:
            canvas.line(players_pts, _CYAN, 2)
        elif len(players_pts) == 1:
            canvas.circle(int(players_pts[0][0]), int(players_pts[0][1]), 3, fill=_CYAN)

        # 延迟曲线（离线点断开）
        lat_segments = [[]]
        for idx, r in enumerate(rows):
            if int(r.get("online") or 0) and float(r.get("latency") or -1) >= 0:
                ly = int(cy1 - (min(float(r.get("latency") or 0), latency_max) / latency_max) * ch)
                lat_segments[-1].append((xpos(idx), ly))
            else:
                if lat_segments[-1]:
                    lat_segments.append([])
        for seg in lat_segments:
            if len(seg) >= 2:
                canvas.line(seg, _ORANGE, 2)
            elif len(seg) == 1:
                canvas.circle(int(seg[0][0]), int(seg[0][1]), 3, fill=_ORANGE)

        # 时间轴标签（首/中/尾）
        for idx in (0, n // 2, n - 1):
            x = xpos(idx)
            ts = rows[idx].get("ts")
            label = _fmt_ts(ts)
            tw, _ = canvas.text_metrics(label, 12)
            tx = x - tw / 2
            tx = max(cx0, min(cx1 - tw, tx))
            text(tx, cy1 + 6, label, 12, _TEXT_DIM)
            canvas.line([(int(x), cy1), (int(x), cy1 + 4)], _GRID, 1)

    # 底部信息
    max_rows = _get_int("max_rows", 10000, 100)
    points = _get_int("chart_points", 120, 10, 500)
    footer = f"保留 {max_rows} 条 · 展示最近 {points} 条 · 每 {int(_get_float('probe_timeout', 10, 1, 60))}s 超时 · 更新 {datetime.now().strftime('%H:%M:%S')}"
    text(30, H - 30, footer, 12, _TEXT_DIM)

    return _b64.b64encode(bytes(canvas.to_png())).decode("ascii")


# ═══════════════════════════ 消息回复工具 ═══════════════════════════

def _reply(event, text):
    try:
        if getattr(event, "is_group", False) and getattr(event, "group_id", None):
            ctx.send_msg(group_id=event.group_id, message=text)
        else:
            uid = getattr(event, "user_id", None)
            if uid:
                ctx.send_msg(user_id=uid, message=text)
    except Exception:
        try:
            _log("回复消息失败", "error")
        except Exception:
            pass


def _send_chart(event):
    """渲染并发送心电图图片（异步，避免渲染阻塞）"""
    def _job():
        try:
            points = _get_int("chart_points", 120, 10, 500)
            rows = _recent_rows(points)
            if not rows:
                _reply(event, "暂无检测数据，等待下一次探测（约 1 分钟内）...")
                return
            b64 = render_chart_image(rows)
            _reply(event, f"[CQ:image,file=base64://{b64}]")
        except Exception as e:
            _log(f"渲染图片失败: {e}", "error")
            _reply(event, f"图片渲染失败：{e}")

    threading.Thread(target=_job, daemon=True).start()


# ═══════════════════════════ 文字子命令 ═══════════════════════════

def _cmd_status():
    last = _last_row()
    if not last:
        return "暂无检测数据，等待下一次探测..."
    online = bool(int(last.get("online") or 0))
    ts = int(last.get("ts") or 0)
    players = int(last.get("players") or 0)
    max_players = int(last.get("max_players") or 0)
    latency = float(last.get("latency") or -1)
    version = _clean_str(last.get("version") or "", 40)
    motd = _clean_str(last.get("motd") or "", 60)
    host = _get_str("server_host", "dbcj.top")
    lines = [f"【{host.upper()} 服务器状态】"]
    lines.append(f"时间：{datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"状态：{'✅ 在线' if online else '❌ 离线'}")
    if online:
        lines.append(f"玩家：{players}/{max_players}")
        lines.append(f"延迟：{latency:.0f}ms" if latency >= 0 else "延迟：--")
        if version:
            lines.append(f"版本：{version}")
        if motd:
            lines.append(f"MOTD：{motd}")
    else:
        lines.append("（服务器不可达，请用 /dbcj 测试 手动验证）")
    return "\n".join(lines)


def _cmd_data(text):
    m = re.search(r"(\d+)", text)
    n = max(1, min(50, int(m.group(1)))) if m else 20
    rows = _recent_rows(n)
    if not rows:
        return "暂无检测数据"
    lines = [f"最近 {len(rows)} 条记录："]
    for r in rows:
        ts = int(r.get("ts") or 0)
        online = int(r.get("online") or 0)
        players = int(r.get("players") or 0)
        max_players = int(r.get("max_players") or 0)
        latency = float(r.get("latency") or -1)
        t = datetime.fromtimestamp(ts).strftime("%H:%M")
        if online:
            lines.append(f"· {t} 在线 {players}/{max_players} {latency:.0f}ms")
        else:
            lines.append(f"· {t} 离线")
    return "\n".join(lines)


def _cmd_help():
    return (
        "【DBCJ MC 状态监控】\n"
        "/dbcj 或 /mc —— 发送心电图状态图（人数+延迟同图双线）\n"
        "/dbcj 状态 —— 文字版当前状态\n"
        "/dbcj 数据 [N] —— 最近 N 条记录（默认20）\n"
        "/dbcj 测试 —— 立即探测一次并返回详情\n"
        "/dbcj 设置 —— 查看当前配置\n"
        "/dbcj 设置 <键> <值> —— 修改配置\n"
        "    键：host(地址)/port(端口)/timeout(超时秒)/max_rows(保留行数)/points(图表点数)\n"
        "    例：/dbcj 设置 host mc.example.com\n"
        "/dbcj 重置 —— 恢复默认配置\n"
        "/dbcj 帮助 —— 本帮助\n"
    )


# ═══════════════════════════ 主命令处理 ═══════════════════════════

def dbcj_handler(event, match):
    """主命令路由：/dbcj 及别名"""
    try:
        msg = str(getattr(event, "message", "") or "").strip()
        # 去掉第一个命令词（如 /dbcj、/mc、/服务器）
        parts = msg.split(maxsplit=1)
        rest = parts[1].strip() if len(parts) > 1 else ""
        rest_l = rest.lower()

        if rest_l in ("状态", "status"):
            _reply(event, _cmd_status())
        elif rest_l.startswith(("数据", "data")):
            _reply(event, _cmd_data(rest))
        elif rest_l in ("测试", "test", "now", "探测", "check"):
            _reply(event, "正在探测服务器，请稍候...")
            _cmd_test_async(event)
        elif rest_l in ("设置", "config", "set", "配置", "cfg"):
            _cmd_config(event, rest)
        elif rest_l in ("重置", "reset", "恢复默认"):
            _reply(event, _reset_conf())
        elif rest_l in ("帮助", "help", "?", "/?", "是什么", "说明"):
            _reply(event, _cmd_help())
        else:
            # 默认：渲染心电图图片
            _send_chart(event)
    except Exception as e:
        _log(f"命令处理异常: {e}", "error")
        try:
            _reply(event, f"处理失败：{e}")
        except Exception:
            pass


def _cmd_config(event, rest):
    """处理 /dbcj 设置 ..."""
    m = re.search(r"设置|config|set|配置|cfg", rest, re.IGNORECASE)
    if m:
        after = rest[m.end():].strip()
    else:
        after = rest.strip()
    if not after:
        _reply(event, _current_conf_text())
        return
    # 解析 键 值
    m2 = re.match(r"^([\w\u4e00-\u9fa5\-\.]+)\s+(.+)$", after)
    if not m2:
        _reply(event, "格式：/dbcj 设置 <键> <值>，例如 /dbcj 设置 host mc.example.com\n"
                      "不带参数则查看当前配置")
        return
    key_raw = m2.group(1).strip().lower()
    value = m2.group(2).strip()
    key = _CONF_ALIAS.get(key_raw, key_raw)
    ok, msg = _set_conf(key, value)
    _reply(event, f"{'✅' if ok else '❌'} {msg}")
    # 若改的是服务器地址/端口，立即后台探测一次
    if ok and key in ("server_host", "server_port"):
        threading.Thread(target=probe_task, daemon=True).start()


def _cmd_test_async(event):
    """立即探测一次，结果异步返回"""
    def _job():
        host = _get_str("server_host", "dbcj.top")
        port = _get_int("server_port", 25565, 1, 65535)
        timeout = _get_float("probe_timeout", 10, 1, 60)
        players = 0
        max_players = 0
        latency = -1.0
        version = ""
        motd = ""
        try:
            status, addr = _do_probe(host, port, timeout)
            players = int(getattr(status.players, "online", 0) or 0)
            max_players = int(getattr(status.players, "max", 0) or 0)
            latency = float(getattr(status, "latency", -1) or -1)
            version = _clean_str(getattr(getattr(status, "version", None), "name", ""))
            motd = _motd_plain(getattr(status, "motd", None))
            addr_txt = f"\n解析地址：{addr}" if addr else ""
            txt = (
                f"✅ 探测成功\n"
                f"服务器：{host}:{port}\n"
                f"玩家：{players}/{max_players}\n"
                f"延迟：{latency:.0f}ms\n"
                f"版本：{version or '未知'}\n"
                f"MOTD：{motd or '无'}{addr_txt}"
            )
        except Exception as e:
            txt = f"❌ 探测失败：{host}:{port}\n原因：{e}\n（可能服务器离线、超时、或 SRV/DNS 解析失败）"
        _reply(event, txt)
        # 无论成败都记一条库（保证图表有数据）
        try:
            _record(success, players, max_players, latency, version, motd)
        except Exception:
            pass

    threading.Thread(target=_job, daemon=True).start()


# ═══════════════════════════ 注册 ═══════════════════════════

def register(ctx_):
    global ctx
    ctx = ctx_
    # 建表
    try:
        ctx.db_execute(_CREATE_DATA_TABLE)
        ctx.db_execute(_CREATE_CONF_TABLE)
        _log("数据表就绪", "info")
    except Exception as e:
        _log(f"建表失败: {e}", "error")
    # 加载运行期配置覆盖
    _load_conf_overrides()
    # 注册命令（/dbcj + 多个别名）
    ctx.command(
        "/dbcj",
        dbcj_handler,
        priority=50,
        alias=["/mc", "/mcstatus", "/服务器", "/mc状态", "/mc状态图"],
        description="DBCJ MC 服务器状态：发送状态图/状态/数据/设置/测试/帮助",
        require_admin=False,
        require_superuser=False,
    )
    # 注册定时探测任务（默认每分钟）
    cron = _get_str("probe_cron", "* * * * *")
    try:
        ctx.task(cron, probe_task)
        _log(f"定时任务已注册: {cron} → probe_task", "info")
    except Exception as e:
        _log(f"定时任务注册失败: {e}", "error")
    # 启动后 3 秒立即探测一次，避免干等第一分钟
    threading.Timer(3, probe_task).start()
    _log("DBCJ-MCStatus 插件已加载", "info")

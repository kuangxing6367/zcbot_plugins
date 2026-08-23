"""
qqadmin 群管插件 - 扩展子系统
==============================
在原有群管能力之上补齐四组功能，均按 zcbot 规范实现：

1. 群打卡（send_group_sign）
   立即打卡 / 一键打卡 / 添加打卡 / 查看打卡 / 删除打卡 / 打卡统计 / 打卡帮助
   - 每日防重复，按分钟粒度的定时任务驱动
   - 批量打卡带间隔，降低接口频率

2. 群数据导出
   导出群数据 / 导出所有群数据
   - 优先输出 xlsx（openpyxl 可用时），否则回退 UTF-8 BOM 的 CSV
   - 通过 upload_group_file / upload_private_file 回传，base64 失败时退回本地路径

3. 群事件监听通知
   群事件监听 / 开启|关闭群事件监听 / 开启|关闭管理员变更通知 /
   开启|关闭禁言通知 / 开启|关闭撤回通知 / 开启|关闭显示操作者
   - 订阅 notice.group_admin、notice.group_ban、notice.group_recall

4. 群开关机
   开机 / 关机 / 查机 / 开机列表
   - 关机后本群消息在原始消息层被拦截，管理员与开机指令放行

配置持久化复用 store.GroupConfigStore（按群继承 default）。
"""
import asyncio
import base64
import csv
import io
import os
import re
import time
from datetime import datetime

import guard
import store

# 由 main.register 注入
ctx = None

# ── 打卡数据表 ──
_DDL_SIGN_JOBS = """
CREATE TABLE IF NOT EXISTS qqadmin_sign_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id BIGINT NOT NULL,
    sign_time VARCHAR(8) NOT NULL,
    creator BIGINT NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE(group_id)
)
"""

_DDL_SIGN_DAILY = """
CREATE TABLE IF NOT EXISTS qqadmin_sign_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id BIGINT NOT NULL,
    day VARCHAR(16) NOT NULL,
    signed_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE(group_id, day)
)
"""

_DDL_SIGN_STAT = """
CREATE TABLE IF NOT EXISTS qqadmin_sign_stat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day VARCHAR(16) NOT NULL,
    success INTEGER NOT NULL DEFAULT 0,
    fail INTEGER NOT NULL DEFAULT 0,
    UNIQUE(day)
)
"""

# 关机提示冷却：group_id -> 上次提示时间
_power_tip_at = {}
# 关机提示冷却秒数
_POWER_TIP_CD = 60


# ===================== 基础工具 =====================

def _log(msg, level="warning"):
    """统一日志输出"""
    try:
        getattr(ctx.logger, level)(f"[群管扩展] {msg}")
    except Exception:
        pass


def _store():
    return store.GroupConfigStore(ctx)


def _cfg(gid):
    return store.GroupConfigStore(ctx).get_group(gid)


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _fmt_ts(ts, fmt="%Y-%m-%d %H:%M"):
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        return "—"
    if ts <= 0:
        return "—"
    try:
        return datetime.fromtimestamp(ts).strftime(fmt)
    except Exception:
        return "—"


async def _areply(event, message):
    """异步统一回复"""
    try:
        await ctx.asend_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message=message,
        )
    except Exception as e:
        _log(f"回复失败: {e}")


def _send_group(gid, message):
    try:
        ctx.api("send_group_msg", group_id=int(gid), message=message)
    except Exception as e:
        _log(f"群消息发送失败 gid={gid}: {e}")


async def _asend_group(gid, message):
    try:
        await ctx.aapi("send_group_msg", group_id=int(gid), message=message)
    except Exception as e:
        _log(f"群消息发送失败 gid={gid}: {e}")


def _can_manage(event):
    """管理员/群主/协管/超管"""
    gid = getattr(event, "group_id", None)
    cfg = _cfg(gid) if gid else {}
    try:
        return guard.can_manage(event, cfg)
    except Exception:
        return getattr(event, "role", "") in ("super", "owner", "admin")


def _is_super(event):
    return getattr(event, "role", "") == "super"


async def _require_manage(event, need_super=False):
    """权限守卫：不足时回复并返回 False"""
    if need_super:
        if not _is_super(event):
            await _areply(event, "⛔ 该指令仅超级管理员可用")
            return False
        return True
    if not _can_manage(event):
        await _areply(event, "⛔ 权限不足，需要管理员、协管或以上权限")
        return False
    return True


def _arg(match, index=1):
    """安全读取正则捕获组"""
    if not match:
        return ""
    try:
        return (match.group(index) or "").strip()
    except (IndexError, AttributeError):
        return ""


def _upsert(update_sql, update_params, insert_sql, insert_params, tag=""):
    """通用 UPDATE→INSERT 幂等写入（避免依赖数据库方言的 UPSERT 语法）"""
    try:
        affected = ctx.db_execute(update_sql, update_params)
        if affected:
            return True
        ctx.db_execute(insert_sql, insert_params)
        return True
    except Exception as e:
        _log(f"写入失败{('(' + tag + ')') if tag else ''}: {e}")
        return False


# ===================== 一、群打卡 =====================

def _ensure_sign_tables():
    for ddl in (_DDL_SIGN_JOBS, _DDL_SIGN_DAILY, _DDL_SIGN_STAT):
        try:
            ctx.create_table(ddl)
        except Exception as e:
            _log(f"打卡建表失败: {e}")


def _sign_signed_today(gid):
    """本群今日是否已打卡"""
    try:
        row = ctx.db_query_one(
            "SELECT id FROM qqadmin_sign_daily WHERE group_id=%s AND day=%s",
            [int(gid), _today()],
        )
        return bool(row)
    except Exception:
        return False


def _sign_mark_today(gid):
    try:
        ctx.db_execute(
            "INSERT INTO qqadmin_sign_daily (group_id, day, signed_at) VALUES (%s, %s, %s)",
            [int(gid), _today(), int(time.time())],
        )
    except Exception as e:
        _log(f"记录打卡失败 gid={gid}: {e}")


def _sign_bump_stat(success=0, fail=0):
    day = _today()
    _upsert(
        "UPDATE qqadmin_sign_stat SET success=success+%s, fail=fail+%s WHERE day=%s",
        [int(success), int(fail), day],
        "INSERT INTO qqadmin_sign_stat (day, success, fail) VALUES (%s, %s, %s)",
        [day, int(success), int(fail)],
        tag="打卡统计",
    )


async def _do_sign(gid):
    """
    执行单群打卡。
    返回 (状态, 说明)，状态取值：ok / already / fail
    """
    if _sign_signed_today(gid):
        return "already", "今日已打卡"
    try:
        await ctx.aapi("send_group_sign", group_id=int(gid))
    except Exception as e:
        _sign_bump_stat(fail=1)
        return "fail", str(e)
    _sign_mark_today(gid)
    _sign_bump_stat(success=1)
    return "ok", "打卡成功"


def _sign_jobs():
    try:
        return ctx.db_query(
            "SELECT group_id, sign_time, creator, created_at FROM qqadmin_sign_jobs"
            " ORDER BY sign_time ASC"
        ) or []
    except Exception as e:
        _log(f"查询打卡任务失败: {e}")
        return []


def _sign_interval():
    """批量打卡间隔秒数"""
    try:
        return max(0, int(ctx.get_config("sign_batch_interval", 2)))
    except Exception:
        return 2


async def handle_sign_now(event, match):
    """立即打卡 [群号]"""
    if not await _require_manage(event):
        return
    arg = _arg(match)
    gid = int(arg) if arg.isdigit() else getattr(event, "group_id", None)
    if not gid:
        await _areply(event, "请在群聊中使用，或指定群号：立即打卡 123456789")
        return
    state, detail = await _do_sign(gid)
    if state == "ok":
        await _areply(event, f"✅ 群 {gid} 打卡成功")
    elif state == "already":
        await _areply(event, f"📅 群 {gid} 今日已打卡，无需重复")
    else:
        await _areply(event, f"❌ 群 {gid} 打卡失败：{detail}")


async def handle_sign_all(event, match):
    """一键打卡：对所有已配置定时打卡的群执行一次"""
    if not await _require_manage(event, need_super=True):
        return
    jobs = _sign_jobs()
    if not jobs:
        await _areply(event, "还没有配置定时打卡的群\n用法：添加打卡 09:00 123456789")
        return
    interval = _sign_interval()
    await _areply(event, f"🚀 开始一键打卡，共 {len(jobs)} 个群，间隔 {interval}s…")
    ok = already = fail = 0
    fail_detail = []
    for job in jobs:
        gid = job.get("group_id")
        state, detail = await _do_sign(gid)
        if state == "ok":
            ok += 1
        elif state == "already":
            already += 1
        else:
            fail += 1
            if len(fail_detail) < 5:
                fail_detail.append(f"{gid}: {detail[:40]}")
        if interval > 0:
            try:
                await asyncio.sleep(interval)
            except Exception:
                break
    lines = [
        "📋 一键打卡完成",
        f"成功 {ok} 个 · 已打卡 {already} 个 · 失败 {fail} 个",
    ]
    if fail_detail:
        lines.append("失败明细：")
        lines.extend(f"  · {d}" for d in fail_detail)
    await _areply(event, "\n".join(lines))


async def handle_sign_add(event, match):
    """添加打卡 <HH:MM> [群号]"""
    if not await _require_manage(event):
        return
    text = _arg(match)
    m = re.match(r"^(\d{1,2})[:：](\d{1,2})\s*(\d*)$", text)
    if not m:
        await _areply(event, "用法：添加打卡 09:00 [群号]\n群号省略时使用当前群")
        return
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await _areply(event, "时间不合法，小时 0-23、分钟 0-59")
        return
    gid_text = m.group(3)
    gid = int(gid_text) if gid_text else getattr(event, "group_id", None)
    if not gid:
        await _areply(event, "请在群聊中使用，或显式指定群号")
        return
    # 非超管只能配置自己所在的当前群，避免越权操作他群
    if gid_text and int(gid_text) != (getattr(event, "group_id", 0) or 0) and not _is_super(event):
        await _areply(event, "⛔ 仅超级管理员可为其他群配置定时打卡")
        return

    sign_time = f"{hour:02d}:{minute:02d}"
    ok = _upsert(
        "UPDATE qqadmin_sign_jobs SET sign_time=%s, creator=%s WHERE group_id=%s",
        [sign_time, int(event.user_id), int(gid)],
        "INSERT INTO qqadmin_sign_jobs (group_id, sign_time, creator, created_at)"
        " VALUES (%s, %s, %s, %s)",
        [int(gid), sign_time, int(event.user_id), int(time.time())],
        tag="打卡任务",
    )
    if not ok:
        await _areply(event, "保存失败，请查看日志")
        return
    try:
        ctx.audit_log(
            action="qqadmin_sign_add",
            target_type="group",
            target_name=str(gid),
            detail={"time": sign_time, "operator": event.user_id},
        )
    except Exception:
        pass
    await _areply(event, f"✅ 已设置群 {gid} 每天 {sign_time} 自动打卡")


async def handle_sign_list(event, match):
    """查看打卡：列出全部定时打卡任务"""
    if not await _require_manage(event):
        return
    jobs = _sign_jobs()
    if not jobs:
        await _areply(event, "暂无定时打卡任务\n用法：添加打卡 09:00 [群号]")
        return
    today = _today()
    try:
        rows = ctx.db_query(
            "SELECT group_id FROM qqadmin_sign_daily WHERE day=%s", [today]
        ) or []
        signed = {str(r.get("group_id")) for r in rows}
    except Exception:
        signed = set()
    lines = [f"⏰ 定时打卡任务（{len(jobs)} 个）", "━━━━━━━━━━━━━━"]
    for job in jobs:
        gid = job.get("group_id")
        flag = "✅今日已打" if str(gid) in signed else "⏳待执行"
        lines.append(f"· 群 {gid} → {job.get('sign_time')}  {flag}")
    lines.append("━━━━━━━━━━━━━━")
    lines.append("删除：删除打卡 <群号>")
    await _areply(event, "\n".join(lines))


async def handle_sign_del(event, match):
    """删除打卡 <群号>"""
    if not await _require_manage(event):
        return
    arg = _arg(match)
    gid = int(arg) if arg.isdigit() else getattr(event, "group_id", None)
    if not gid:
        await _areply(event, "用法：删除打卡 <群号>")
        return
    if arg and int(arg) != (getattr(event, "group_id", 0) or 0) and not _is_super(event):
        await _areply(event, "⛔ 仅超级管理员可删除其他群的打卡任务")
        return
    try:
        affected = ctx.db_execute(
            "DELETE FROM qqadmin_sign_jobs WHERE group_id=%s", [int(gid)]
        )
    except Exception as e:
        _log(f"删除打卡任务失败: {e}")
        await _areply(event, "删除失败，请查看日志")
        return
    if affected:
        await _areply(event, f"🗑 已删除群 {gid} 的定时打卡任务")
    else:
        await _areply(event, f"群 {gid} 没有定时打卡任务")


async def handle_sign_stat(event, match):
    """打卡统计：总计 + 今日 + 配置概况"""
    if not await _require_manage(event):
        return
    total_ok = total_fail = 0
    try:
        row = ctx.db_query_one(
            "SELECT COALESCE(SUM(success),0) AS s, COALESCE(SUM(fail),0) AS f"
            " FROM qqadmin_sign_stat", []
        ) or {}
        total_ok = int(row.get("s") or 0)
        total_fail = int(row.get("f") or 0)
    except Exception as e:
        _log(f"查询打卡总统计失败: {e}")
    today_ok = today_fail = 0
    try:
        row = ctx.db_query_one(
            "SELECT success, fail FROM qqadmin_sign_stat WHERE day=%s", [_today()]
        ) or {}
        today_ok = int(row.get("success") or 0)
        today_fail = int(row.get("fail") or 0)
    except Exception:
        pass
    jobs = _sign_jobs()
    last = "—"
    try:
        row = ctx.db_query_one(
            "SELECT group_id, signed_at FROM qqadmin_sign_daily"
            " ORDER BY signed_at DESC LIMIT 1", []
        ) or {}
        if row.get("signed_at"):
            last = f"{_fmt_ts(row.get('signed_at'))}（群 {row.get('group_id')}）"
    except Exception:
        pass
    lines = [
        "📊 群打卡统计",
        "━━━━━━━━━━━━━━",
        f"累计成功：{total_ok} 次",
        f"累计失败：{total_fail} 次",
        f"今日成功：{today_ok} 次 · 今日失败：{today_fail} 次",
        f"定时任务：{len(jobs)} 个群",
        f"最近打卡：{last}",
        f"批量间隔：{_sign_interval()} 秒",
    ]
    await _areply(event, "\n".join(lines))


_SIGN_HELP = """✅ 群打卡使用说明
━━━━━━━━━━━━━━
▸ 立即打卡 [群号]        为当前群或指定群立即打卡
▸ 一键打卡               对所有定时任务群依次打卡（超管）
▸ 添加打卡 09:00 [群号]  设置每日定时打卡
▸ 查看打卡               查看全部定时任务
▸ 删除打卡 <群号>        删除定时任务
▸ 打卡统计               查看成功/失败统计
━━━━━━━━━━━━━━
📌 说明
· 每群每天只会成功打卡一次，重复触发提示"今日已打卡"
· 定时任务按分钟精度执行，依赖框架定时调度
· 依赖实现端提供 send_group_sign 接口（如 NapCat）"""


async def handle_sign_help(event, match):
    await _areply(event, _SIGN_HELP)


async def sign_tick():
    """定时任务：每分钟检查是否有到点的打卡任务"""
    now = datetime.now().strftime("%H:%M")
    jobs = [j for j in _sign_jobs() if str(j.get("sign_time")) == now]
    if not jobs:
        return
    interval = _sign_interval()
    notify = True
    try:
        notify = bool(ctx.get_config("sign_notify", True))
    except Exception:
        pass
    for job in jobs:
        gid = job.get("group_id")
        state, detail = await _do_sign(gid)
        if state == "fail":
            _log(f"定时打卡失败 gid={gid}: {detail}")
        elif state == "ok":
            _log(f"定时打卡成功 gid={gid}", "info")
            if notify:
                await _asend_group(gid, "✅ 本群今日打卡已完成")
        if interval > 0:
            try:
                await asyncio.sleep(interval)
            except Exception:
                break


# ===================== 二、群数据导出 =====================

_EXPORT_COLUMNS = [
    ("user_id", "QQ号"),
    ("nickname", "昵称"),
    ("card", "群名片"),
    ("sex", "性别"),
    ("age", "年龄"),
    ("area", "地区"),
    ("level", "群等级"),
    ("role", "身份"),
    ("title", "头衔"),
    ("join_time", "入群时间"),
    ("last_sent_time", "最后发言"),
    ("shut_up_timestamp", "禁言到期"),
    ("unfriendly", "不良记录"),
    ("card_changeable", "可改名片"),
]

_ROLE_CN = {"owner": "群主", "admin": "管理员", "member": "成员"}
_SEX_CN = {"male": "男", "female": "女", "unknown": "未知"}

# Excel 不接受的控制字符
_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean_cell(value):
    """清理单元格内容：去掉 Excel 非法控制字符"""
    if isinstance(value, str):
        return _ILLEGAL_RE.sub("", value)
    return value


def _norm_member(member):
    """把成员原始字段整理为可读行"""
    row = {}
    for key, _label in _EXPORT_COLUMNS:
        value = member.get(key)
        if key in ("join_time", "last_sent_time", "shut_up_timestamp"):
            row[key] = _fmt_ts(value, "%Y-%m-%d %H:%M:%S") if value else "—"
        elif key == "role":
            row[key] = _ROLE_CN.get(str(value), str(value or "-"))
        elif key == "sex":
            row[key] = _SEX_CN.get(str(value), str(value or "-"))
        elif key in ("unfriendly", "card_changeable"):
            row[key] = "是" if value else "否"
        else:
            row[key] = _clean_cell(value if value not in (None, "") else "—")
    return row


def _build_xlsx(sheets):
    """
    用 openpyxl 生成 xlsx 字节流。
    sheets: [(sheet_name, extra_headers, rows)]，rows 为 _norm_member 结果列表
    openpyxl 不可用时返回 None，由调用方回退 CSV。
    """
    try:
        from openpyxl import Workbook
    except Exception:
        return None
    wb = Workbook()
    wb.remove(wb.active)
    for name, extra_headers, rows in sheets:
        safe = re.sub(r"[\[\]\*/\\\?:]", "_", str(name))[:31] or "Sheet"
        ws = wb.create_sheet(safe)
        headers = [h for _k, h in _EXPORT_COLUMNS]
        ws.append(list(extra_headers.keys()) + headers if extra_headers else headers)
        for row in rows:
            prefix = list(extra_headers.values()) if extra_headers else []
            ws.append(prefix + [row.get(k, "") for k, _h in _EXPORT_COLUMNS])
        # 简单列宽自适应
        for idx in range(1, ws.max_column + 1):
            ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = 16
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_csv(rows, extra_headers=None):
    """回退方案：生成带 BOM 的 CSV，Excel 双击可直接正确打开"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    prefix_keys = list((extra_headers or {}).keys())
    writer.writerow(prefix_keys + [h for _k, h in _EXPORT_COLUMNS])
    for row in rows:
        prefix = [row.get(f"__{k}", "") for k in prefix_keys] if prefix_keys else []
        writer.writerow(prefix + [row.get(k, "") for k, _h in _EXPORT_COLUMNS])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


async def _fetch_members(gid):
    """拉取群成员列表"""
    try:
        data = await ctx.aapi("get_group_member_list", group_id=int(gid), no_cache=True)
    except Exception:
        data = await ctx.aapi("get_group_member_list", group_id=int(gid))
    if isinstance(data, dict):
        data = data.get("data") or []
    return [m for m in (data or []) if isinstance(m, dict)]


async def _upload_file(event, content, filename):
    """
    回传文件：优先 base64 直传，失败则落盘到插件数据目录后按本地路径上传。
    返回 (是否成功, 兜底本地路径或 None)
    """
    is_group = bool(getattr(event, "is_group", False) and getattr(event, "group_id", None))
    action = "upload_group_file" if is_group else "upload_private_file"
    key = "group_id" if is_group else "user_id"
    target = int(event.group_id) if is_group else int(event.user_id)

    b64 = base64.b64encode(content).decode("utf-8")
    try:
        await ctx.aapi(action, **{key: target, "file": f"base64://{b64}", "name": filename})
        return True, None
    except Exception as e:
        _log(f"base64 上传失败，改用本地路径: {e}", "info")

    path = None
    try:
        data_dir = os.path.join(ctx.get_data_dir(), "export")
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, filename)
        with open(path, "wb") as f:
            f.write(content)
        await ctx.aapi(action, **{key: target, "file": path, "name": filename})
        return True, path
    except Exception as e:
        _log(f"本地路径上传失败: {e}")
        return False, path


async def handle_export_group(event, match):
    """导出群数据 [群号]"""
    if not await _require_manage(event):
        return
    arg = _arg(match)
    gid = int(arg) if arg.isdigit() else getattr(event, "group_id", None)
    if not gid:
        await _areply(event, "请在群聊中使用，或指定群号：导出群数据 123456789")
        return
    if arg and int(arg) != (getattr(event, "group_id", 0) or 0) and not _is_super(event):
        await _areply(event, "⛔ 仅超级管理员可导出其他群的数据")
        return

    await _areply(event, f"📤 正在导出群 {gid} 成员数据，请稍候…")
    try:
        members = await _fetch_members(gid)
    except Exception as e:
        await _areply(event, f"获取群成员失败：{e}")
        return
    if not members:
        await _areply(event, "没有取到群成员数据")
        return

    rows = [_norm_member(m) for m in members]
    payload = _build_xlsx([(f"群{gid}", None, rows)])
    ext = "xlsx"
    if payload is None:
        payload = _build_csv(rows)
        ext = "csv"
    filename = f"群{gid}成员数据_{len(rows)}人_{datetime.now():%Y%m%d%H%M}.{ext}"

    ok, path = await _upload_file(event, payload, filename)
    if ok:
        tip = f"✅ 已导出 {len(rows)} 名成员：{filename}"
        if ext == "csv":
            tip += "\n（未安装 openpyxl，已回退 CSV 格式）"
        await _areply(event, tip)
    else:
        tip = "❌ 文件上传失败"
        if path:
            tip += f"，已保存到本地：{path}"
        await _areply(event, tip)


async def handle_export_all(event, match):
    """导出所有群数据（超管）"""
    if not await _require_manage(event, need_super=True):
        return
    await _areply(event, "📤 正在导出所有群数据，群数较多时耗时较长，请稍候…")
    try:
        groups = await ctx.aapi("get_group_list", no_cache=True)
    except Exception:
        try:
            groups = await ctx.aapi("get_group_list")
        except Exception as e:
            await _areply(event, f"获取群列表失败：{e}")
            return
    if isinstance(groups, dict):
        groups = groups.get("data") or []
    groups = [g for g in (groups or []) if isinstance(g, dict)]
    if not groups:
        await _areply(event, "没有取到群列表")
        return

    sheets = []
    flat_rows = []
    total = 0
    failed = []
    for g in groups:
        gid = g.get("group_id")
        gname = str(g.get("group_name") or gid)
        if not gid:
            continue
        try:
            members = await _fetch_members(gid)
        except Exception as e:
            failed.append(f"{gid}: {str(e)[:30]}")
            continue
        rows = [_norm_member(m) for m in members]
        total += len(rows)
        sheets.append((f"{gid}", {"群号": gid, "群名": _clean_cell(gname)}, rows))
        for row in rows:
            merged = dict(row)
            merged["__群号"] = gid
            merged["__群名"] = _clean_cell(gname)
            flat_rows.append(merged)
        try:
            await asyncio.sleep(0.5)
        except Exception:
            break

    if not sheets:
        await _areply(event, "所有群成员拉取均失败，请检查实现端接口权限")
        return

    payload = _build_xlsx(sheets)
    ext = "xlsx"
    if payload is None:
        payload = _build_csv(flat_rows, extra_headers={"群号": "", "群名": ""})
        ext = "csv"
    filename = f"全部群数据_{len(sheets)}群{total}人_{datetime.now():%Y%m%d%H%M}.{ext}"

    ok, path = await _upload_file(event, payload, filename)
    lines = []
    if ok:
        lines.append(f"✅ 已导出 {len(sheets)} 个群、{total} 名成员：{filename}")
        if ext == "csv":
            lines.append("（未安装 openpyxl，已回退单表 CSV）")
    else:
        lines.append("❌ 文件上传失败")
        if path:
            lines.append(f"已保存到本地：{path}")
    if failed:
        lines.append(f"以下 {len(failed)} 个群拉取失败：")
        lines.extend(f"  · {d}" for d in failed[:5])
    await _areply(event, "\n".join(lines))


# ===================== 三、群事件监听通知 =====================

_WATCH_KEYS = {
    "watch_enabled": "群事件监听",
    "watch_admin_notify": "管理员变更通知",
    "watch_ban_notify": "禁言通知",
    "watch_recall_notify": "撤回通知",
    "watch_show_operator": "显示操作者",
}


def _member_name(gid, uid):
    """取群成员显示名，失败回退 QQ 号"""
    try:
        info = ctx.get_member_info(gid, uid) or {}
        return info.get("card") or info.get("nickname") or str(uid)
    except Exception:
        return str(uid)


def _watch_on(cfg, key):
    """监听子项是否生效：总开关 + 子开关都为真"""
    if not cfg.get("watch_enabled"):
        return False
    return bool(cfg.get(key, True))


def _who(cfg, gid, uid, label="操作者"):
    """按配置决定是否展示操作者"""
    if not uid or not cfg.get("watch_show_operator", True):
        return ""
    return f"\n{label}：{_member_name(gid, uid)}({uid})"


def _dur_text(seconds):
    """秒转可读时长"""
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return "未知"
    if seconds <= 0:
        return "0 秒"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分")
    if secs and not days:
        parts.append(f"{secs}秒")
    return "".join(parts) or "0 秒"


async def on_notice_admin(payload):
    """notice.group_admin：管理员被设置/取消"""
    gid = payload.get("group_id")
    uid = payload.get("user_id")
    if not gid or not uid:
        return
    cfg = _cfg(gid)
    if not _watch_on(cfg, "watch_admin_notify"):
        return
    sub = payload.get("sub_type", "")
    name = _member_name(gid, uid)
    if sub == "set":
        text = f"🎖 管理员变更\n{name}({uid}) 已被设为管理员"
    elif sub == "unset":
        text = f"📉 管理员变更\n{name}({uid}) 已被取消管理员"
    else:
        return
    await _asend_group(gid, text)


async def on_notice_ban(payload):
    """notice.group_ban：成员被禁言/解除禁言"""
    gid = payload.get("group_id")
    uid = payload.get("user_id")
    if not gid:
        return
    cfg = _cfg(gid)
    if not _watch_on(cfg, "watch_ban_notify"):
        return
    sub = payload.get("sub_type", "")
    operator = payload.get("operator_id")
    duration = payload.get("duration", 0)

    # user_id 为 0 表示全体禁言开关
    if not uid or int(uid) == 0:
        if sub == "ban":
            text = "🔇 本群已开启全体禁言" + _who(cfg, gid, operator)
        else:
            text = "🔊 本群已解除全体禁言" + _who(cfg, gid, operator)
        await _asend_group(gid, text)
        return

    name = _member_name(gid, uid)
    if sub == "ban":
        text = (f"🔇 禁言通知\n{name}({uid}) 被禁言 {_dur_text(duration)}"
                + _who(cfg, gid, operator))
    elif sub == "lift_ban":
        text = f"🔊 解禁通知\n{name}({uid}) 已被解除禁言" + _who(cfg, gid, operator)
    else:
        return
    await _asend_group(gid, text)


async def on_notice_recall(payload):
    """notice.group_recall：群消息被撤回"""
    gid = payload.get("group_id")
    uid = payload.get("user_id")
    if not gid or not uid:
        return
    cfg = _cfg(gid)
    if not _watch_on(cfg, "watch_recall_notify"):
        return
    operator = payload.get("operator_id")
    name = _member_name(gid, uid)
    lines = [f"🗑 撤回通知\n{name}({uid}) 撤回了一条消息"]
    if operator and str(operator) != str(uid):
        lines[0] = f"🗑 撤回通知\n{name}({uid}) 的一条消息被撤回"
        lines.append(_who(cfg, gid, operator).lstrip("\n"))

    # 尝试还原被撤回的内容（部分实现端支持 get_msg）
    content = await _recall_content(payload.get("message_id"))
    if content:
        limit = 200
        snippet = content if len(content) <= limit else content[:limit] + "…"
        lines.append(f"内容：{snippet}")
    await _asend_group(gid, "\n".join(x for x in lines if x))


async def _recall_content(message_id):
    """尽力还原撤回消息的纯文本，取不到时返回空串"""
    if not message_id:
        return ""
    try:
        data = await ctx.aapi("get_msg", message_id=message_id)
    except Exception:
        return ""
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        return ""
    message = data.get("message")
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, list):
        parts = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type")
            seg_data = seg.get("data") or {}
            if seg_type == "text":
                parts.append(str(seg_data.get("text") or ""))
            elif seg_type == "image":
                parts.append("[图片]")
            elif seg_type == "at":
                parts.append(f"@{seg_data.get('qq')}")
            elif seg_type == "face":
                parts.append("[表情]")
            elif seg_type in ("record", "video"):
                parts.append("[语音/视频]")
        return "".join(parts).strip()
    return ""


async def handle_watch_status(event, match):
    """群事件监听：查看当前设置"""
    gid = getattr(event, "group_id", None)
    if not gid:
        await _areply(event, "请在群聊中使用")
        return
    cfg = _cfg(gid)
    lines = [f"🔭 群事件监听设置（群 {gid}）", "━━━━━━━━━━━━━━"]
    for key, label in _WATCH_KEYS.items():
        state = "✅ 开" if cfg.get(key, key != "watch_enabled") else "❌ 关"
        lines.append(f"{label}：{state}")
    lines.append("━━━━━━━━━━━━━━")
    lines.append("开启群事件监听 / 关闭群事件监听")
    lines.append("开启禁言通知 / 关闭撤回通知 …")
    await _areply(event, "\n".join(lines))


async def _set_watch(event, key, value):
    """写入监听开关"""
    if not await _require_manage(event):
        return
    gid = getattr(event, "group_id", None)
    if not gid:
        await _areply(event, "请在群聊中使用")
        return
    _store().set_group(gid, key, bool(value))
    label = _WATCH_KEYS.get(key, key)
    await _areply(event, f"{label} 已{'开启' if value else '关闭'}")


async def handle_watch_on(event, match):
    await _set_watch(event, "watch_enabled", True)


async def handle_watch_off(event, match):
    await _set_watch(event, "watch_enabled", False)


async def handle_watch_admin_on(event, match):
    await _set_watch(event, "watch_admin_notify", True)


async def handle_watch_admin_off(event, match):
    await _set_watch(event, "watch_admin_notify", False)


async def handle_watch_ban_on(event, match):
    await _set_watch(event, "watch_ban_notify", True)


async def handle_watch_ban_off(event, match):
    await _set_watch(event, "watch_ban_notify", False)


async def handle_watch_recall_on(event, match):
    await _set_watch(event, "watch_recall_notify", True)


async def handle_watch_recall_off(event, match):
    await _set_watch(event, "watch_recall_notify", False)


async def handle_watch_operator_on(event, match):
    await _set_watch(event, "watch_show_operator", True)


async def handle_watch_operator_off(event, match):
    await _set_watch(event, "watch_show_operator", False)


# ===================== 四、群开关机 =====================

def _raw_text(raw):
    """从原始事件里提取纯文本"""
    message = raw.get("message")
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append(str((seg.get("data") or {}).get("text") or ""))
        return "".join(parts)
    return str(raw.get("raw_message") or "")


def _raw_at_bot(raw):
    """原始事件里是否 @ 了机器人"""
    self_id = str(raw.get("self_id") or "")
    if not self_id:
        return False
    message = raw.get("message")
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "at":
                if str((seg.get("data") or {}).get("qq")) in (self_id, "all"):
                    return True
    text = str(raw.get("raw_message") or "")
    return f"[CQ:at,qq={self_id}]" in text


_POWER_PASS_RE = re.compile(r"^/?(开机|查机|开机列表|群管帮助)\s*$")


async def on_raw_power(raw, bot_name=None):
    """
    原始消息钩子：本群处于关机状态时拦截消息。
    返回 True 表示消息被接管（框架不再继续处理）。
    放行条件：开机指令 / 群主 / 管理员 / 协管 / 超管。
    """
    try:
        if raw.get("post_type") != "message" or raw.get("message_type") != "group":
            return False
        gid = raw.get("group_id")
        uid = raw.get("user_id")
        if not gid or not uid:
            return False
        cfg = _cfg(gid)
        if cfg.get("power_enabled", True):
            return False
        if not cfg.get("power_intercept", True):
            return False

        text = _raw_text(raw).strip()
        if _POWER_PASS_RE.match(text):
            return False

        role = str(((raw.get("sender") or {}).get("role")) or "")
        if role in ("owner", "admin"):
            return False
        try:
            if ctx.is_superuser(uid):
                return False
        except Exception:
            pass
        if store.is_assistant(cfg, uid):
            return False

        # 被 @ 时给出一次提示，避免关机后完全无反馈；带冷却防刷
        if _raw_at_bot(raw):
            now = time.time()
            if now - _power_tip_at.get(str(gid), 0) >= _POWER_TIP_CD:
                _power_tip_at[str(gid)] = now
                await _asend_group(gid, "🔒 本群已关机，如需使用请联系群管理员发送「开机」")
        return True
    except Exception as e:
        _log(f"关机拦截异常: {e}")
        return False


async def handle_power_on(event, match):
    """开机：恢复本群响应"""
    if not await _require_manage(event):
        return
    gid = getattr(event, "group_id", None)
    if not gid:
        await _areply(event, "请在群聊中使用")
        return
    _store().set_group(gid, "power_enabled", True)
    _power_tip_at.pop(str(gid), None)
    try:
        ctx.audit_log(
            action="qqadmin_power_on",
            target_type="group",
            target_name=str(gid),
            detail={"operator": event.user_id},
        )
    except Exception:
        pass
    await _areply(event, "✅ 本群已开机，机器人恢复响应")


async def handle_power_off(event, match):
    """关机：本群仅响应管理员"""
    if not await _require_manage(event):
        return
    gid = getattr(event, "group_id", None)
    if not gid:
        await _areply(event, "请在群聊中使用")
        return
    _store().set_group(gid, "power_enabled", False)
    try:
        ctx.audit_log(
            action="qqadmin_power_off",
            target_type="group",
            target_name=str(gid),
            detail={"operator": event.user_id},
        )
    except Exception:
        pass
    await _areply(event, "🔒 本群已关机，普通成员消息将不再响应\n管理员发送「开机」可恢复")


async def handle_power_status(event, match):
    """查机：查看本群开关机状态"""
    gid = getattr(event, "group_id", None)
    if not gid:
        await _areply(event, "请在群聊中使用")
        return
    cfg = _cfg(gid)
    enabled = cfg.get("power_enabled", True)
    intercept = cfg.get("power_intercept", True)
    lines = [
        f"🔌 本群开关机状态（群 {gid}）",
        "━━━━━━━━━━━━━━",
        f"运行状态：{'✅ 已开机' if enabled else '🔒 已关机'}",
        f"关机拦截：{'开启' if intercept else '关闭'}",
    ]
    if not enabled:
        lines.append("管理员发送「开机」即可恢复")
    await _areply(event, "\n".join(lines))


async def handle_power_list(event, match):
    """开机列表：列出已关机的群（其余默认开机）"""
    if not await _require_manage(event, need_super=True):
        return
    st = _store()
    off = []
    for gid in st.all_group_ids():
        cfg = st.get_group(gid)
        if not cfg.get("power_enabled", True):
            off.append(str(gid))
    total = None
    try:
        groups = await ctx.aapi("get_group_list")
        if isinstance(groups, dict):
            groups = groups.get("data") or []
        total = len([g for g in (groups or []) if isinstance(g, dict)])
    except Exception:
        pass

    lines = ["🔌 群开关机总览", "━━━━━━━━━━━━━━"]
    if total is not None:
        lines.append(f"机器人所在群：{total} 个")
        lines.append(f"已开机：{max(0, total - len(off))} 个（默认开机）")
    lines.append(f"已关机：{len(off)} 个")
    if off:
        lines.append("关机群列表：")
        for gid in off[:20]:
            lines.append(f"  · 群 {gid}")
        if len(off) > 20:
            lines.append(f"  · …还有 {len(off) - 20} 个")
    lines.append("━━━━━━━━━━━━━━")
    lines.append("在目标群发送「开机」/「关机」切换")
    await _areply(event, "\n".join(lines))


# ===================== 帮助补充 =====================

EXTRA_HELP_LINES = [
    "## 群打卡",
    "  立即打卡 [群号] / 一键打卡",
    "  添加打卡 09:00 [群号] / 查看打卡 / 删除打卡 <群号>",
    "  打卡统计 / 打卡帮助",
    "## 数据导出",
    "  导出群数据 [群号]",
    "  导出所有群数据 (超管)",
    "## 事件监听",
    "  群事件监听 (查看设置)",
    "  开启|关闭群事件监听",
    "  开启|关闭管理员变更通知",
    "  开启|关闭禁言通知",
    "  开启|关闭撤回通知",
    "  开启|关闭显示操作者",
    "## 开关机",
    "  开机 / 关机 / 查机 / 开机列表",
]


# ===================== 仪表盘卡片 =====================

def dashboard_sign():
    """仪表盘卡片：打卡概览"""
    try:
        row = ctx.db_query_one(
            "SELECT COALESCE(SUM(success),0) AS s FROM qqadmin_sign_stat", []
        ) or {}
        total = int(row.get("s") or 0)
    except Exception:
        total = 0
    try:
        row = ctx.db_query_one(
            "SELECT COUNT(*) AS c FROM qqadmin_sign_jobs", []
        ) or {}
        jobs = int(row.get("c") or 0)
    except Exception:
        jobs = 0
    return {"value": f"{total} 次 / {jobs} 群", "label": "群打卡累计 · 定时任务"}


# ===================== 注册 =====================

def register_extra(ctx_obj):
    """由 main.register 调用，注册扩展子系统的命令、事件与定时任务"""
    global ctx
    ctx = ctx_obj

    _ensure_sign_tables()

    # ── 群打卡 ──
    ctx.command(r"^/?立即打卡\s*(\d*)\s*$", handle_sign_now, priority=50,
                description="为当前群或指定群立即打卡")
    ctx.command(r"^/?一键打卡\s*$", handle_sign_all, priority=50,
                description="对所有定时打卡群依次打卡（超管）")
    ctx.command(r"^/?添加打卡\s+(.+)$", handle_sign_add, priority=50,
                description="添加每日定时打卡：添加打卡 09:00 [群号]")
    ctx.command(r"^/?查看打卡\s*$", handle_sign_list, priority=50,
                alias=[r"^/?打卡列表\s*$"],
                description="查看全部定时打卡任务")
    ctx.command(r"^/?删除打卡\s*(\d*)\s*$", handle_sign_del, priority=50,
                description="删除指定群的定时打卡任务")
    ctx.command(r"^/?打卡统计\s*$", handle_sign_stat, priority=50,
                description="查看群打卡统计")
    ctx.command(r"^/?打卡帮助\s*$", handle_sign_help, priority=50,
                description="群打卡使用说明")

    # ── 数据导出 ──
    ctx.command(r"^/?导出群数据\s*(\d*)\s*$", handle_export_group, priority=50,
                description="导出本群或指定群成员数据（xlsx/csv）")
    ctx.command(r"^/?导出所有群数据\s*$", handle_export_all, priority=50,
                description="导出机器人所在全部群的成员数据（超管）")

    # ── 事件监听 ──
    ctx.command(r"^/?群事件监听\s*$", handle_watch_status, priority=50,
                alias=[r"^/?群事件监听开关\s*$"],
                description="查看群事件监听设置")
    ctx.command(r"^/?开启群事件监听\s*$", handle_watch_on, priority=50,
                description="开启本群事件监听")
    ctx.command(r"^/?关闭群事件监听\s*$", handle_watch_off, priority=50,
                description="关闭本群事件监听")
    ctx.command(r"^/?开启管理员变更通知\s*$", handle_watch_admin_on, priority=50,
                description="开启管理员变更通知")
    ctx.command(r"^/?关闭管理员变更通知\s*$", handle_watch_admin_off, priority=50,
                description="关闭管理员变更通知")
    ctx.command(r"^/?开启禁言通知\s*$", handle_watch_ban_on, priority=50,
                description="开启禁言/解禁通知")
    ctx.command(r"^/?关闭禁言通知\s*$", handle_watch_ban_off, priority=50,
                description="关闭禁言/解禁通知")
    ctx.command(r"^/?开启撤回通知\s*$", handle_watch_recall_on, priority=50,
                alias=[r"^/?开启消息撤回通知\s*$"],
                description="开启消息撤回通知")
    ctx.command(r"^/?关闭撤回通知\s*$", handle_watch_recall_off, priority=50,
                alias=[r"^/?关闭消息撤回通知\s*$"],
                description="关闭消息撤回通知")
    ctx.command(r"^/?开启显示操作者\s*$", handle_watch_operator_on, priority=50,
                description="通知中显示操作者")
    ctx.command(r"^/?关闭显示操作者\s*$", handle_watch_operator_off, priority=50,
                description="通知中隐藏操作者")

    ctx.on("notice.group_admin", on_notice_admin)
    ctx.on("notice.group_ban", on_notice_ban)
    ctx.on("notice.group_recall", on_notice_recall)

    # ── 开关机 ──
    ctx.command(r"^/?开机\s*$", handle_power_on, priority=20,
                description="本群开机，恢复机器人响应")
    ctx.command(r"^/?关机\s*$", handle_power_off, priority=20,
                description="本群关机，普通成员消息不再响应")
    ctx.command(r"^/?查机\s*$", handle_power_status, priority=20,
                alias=[r"^/?开关机状态\s*$"],
                description="查看本群开关机状态")
    ctx.command(r"^/?开机列表\s*$", handle_power_list, priority=20,
                description="查看群开关机总览（超管）")

    ctx.on_raw_message(on_raw_power)

    # ── 定时任务 ──
    ctx.task("* * * * *", sign_tick, description="群管定时打卡检查")

    # ── 仪表盘 ──
    try:
        ctx.dashboard_card("群打卡", dashboard_sign, icon="", priority=40)
    except Exception:
        pass

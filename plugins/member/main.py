"""
成员数据中枢 (member)
=====================
群成员与发言数据的统一中枢：

· 消息流水：记录所有入站消息的纯文本（不接管消息流，消息照常走后续流程），
  提供 /我的发言数 /群发言排行 /发言统计 查询；
· 成员索引：/同步群成员 [群号] 拉取群成员列表写入索引表，
  /查用户 <QQ> 按 QQ 反查所在群，/同步状态 查看索引规模；
· 成员查询：/查成员 <昵称片段> /成员 <QQ> /群成员数，实时查询当前群成员资料；
· CSV 导出：/导出群成员 将本群成员列表导出为 CSV（昵称/名片/角色/入群时间）；
· 卡片输出：发言排行与发言统计优先用「图片渲染器」渲染成卡片图发送，
  渲染插件不存在或渲染异常时自动回退纯文本。

数据持久化（ctx.create_table 建表）：
· user_log(id, group_id, user_id, content, created_at) —— 消息流水
· user_index(group_id, user_id, nickname, role)        —— 成员索引
维护：每日定时任务按配置清理过期流水与过期导出文件。
"""
import asyncio
import csv
import os
import re
import sys
import tempfile
import time

__plugin_meta__ = {
    "name": "成员数据中枢",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "群成员与发言数据中枢：消息流水、成员索引/查询、发言排行卡片图、CSV 导出",
    "priority": 60,
}

ctx = None

# ---------------------------------------------------------------------------
# 数据表
# ---------------------------------------------------------------------------

# 消息流水表：群聊记录 group_id，私聊记 0；content 为纯文本片段
_SQL_CREATE_MSG = """
CREATE TABLE IF NOT EXISTS user_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id BIGINT NOT NULL DEFAULT 0,
    user_id BIGINT NOT NULL,
    content TEXT,
    created_at INTEGER NOT NULL DEFAULT 0
)
"""

# 成员索引表：同步群成员时写入/更新，支持按 QQ 反查所在群
_SQL_CREATE_INDEX = """
CREATE TABLE IF NOT EXISTS user_index (
    group_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    nickname VARCHAR(128) DEFAULT '',
    role VARCHAR(16) DEFAULT 'member',
    PRIMARY KEY (group_id, user_id)
)
"""

# 身份中文映射
_ROLE_CN = {"owner": "群主", "admin": "管理员", "member": "成员"}

# ---------------------------------------------------------------------------
# 配置读取（本地短缓存，避免高频消息逐条穿透配置层）
# ---------------------------------------------------------------------------

_CONF_TTL = 30.0  # 秒；本地配置缓存刷新间隔
_conf_cache = {"data": {}, "ts": 0.0}


def _refresh_conf():
    """刷新本地配置缓存（失败时沿用旧值）"""
    try:
        data = ctx.get_all_config()
        if isinstance(data, dict):
            _conf_cache["data"] = data
            _conf_cache["ts"] = time.time()
    except Exception:
        pass


def _cfg(key, default):
    """读取配置项（带本地 TTL 缓存）"""
    if time.time() - _conf_cache["ts"] > _CONF_TTL:
        _refresh_conf()
    val = _conf_cache["data"].get(key, default)
    return default if val is None else val


def _cfg_int(key, default):
    """读取整型配置（兼容 Web UI 传回字符串）"""
    try:
        return int(float(_cfg(key, default)))
    except (TypeError, ValueError):
        return default


def _cfg_bool(key, default):
    """读取布尔型配置（兼容 Web UI 传回字符串）"""
    val = _cfg(key, default)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on", "开", "开启")


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _text_of(raw):
    """从 OneBot 11 原始事件中提取纯文本（消息段数组时拼接 text 段）"""
    msg = raw.get("message", "")
    if isinstance(msg, list):
        return "".join(
            s.get("data", {}).get("text", "")
            for s in msg
            if isinstance(s, dict) and s.get("type") == "text"
        )
    return str(msg or "")


def _extract_number(text):
    """从参数文本中提取第一个 5 位以上的数字（QQ 号 / 群号）"""
    m = re.search(r"(\d{5,})", text or "")
    return int(m.group(1)) if m else None


def _match_arg(match):
    """安全取命令第一个捕获组（无分组 / 无匹配时返回空串）"""
    if not match:
        return ""
    try:
        return str(match.group(1) or "").strip()
    except Exception:
        return ""


async def _reply(event, msg):
    """回复当前会话（群聊回群，私聊回私）"""
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if getattr(event, "is_group", False) else None,
        message=msg,
    )


def _member_card(m):
    """成员展示名：群名片 > 昵称 > QQ 号"""
    return m.get("card") or m.get("nickname") or str(m.get("user_id", ""))


def _display_name(gid, uid):
    """
    群内用户展示名：优先取成员索引表里最近一次同步的昵称（免接口调用），
    索引未命中再实时调 OneBot 接口，最后退回 QQ 号字面量。
    """
    try:
        rows = ctx.db_query(
            "SELECT nickname FROM user_index WHERE group_id=%s AND user_id=%s LIMIT 1",
            [gid, uid],
        )
        if rows and rows[0].get("nickname"):
            return rows[0]["nickname"]
    except Exception:
        pass
    try:
        info = ctx.get_member_info(gid, uid) or {}
        return info.get("card") or info.get("nickname") or str(uid)
    except Exception:
        return str(uid)


def _fmt_ts(ts):
    """时间戳 → 本地时间字符串；无效值返回 '-'"""
    try:
        ts = int(ts)
        if ts <= 0:
            return "-"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (TypeError, ValueError):
        return "-"


# ---------------------------------------------------------------------------
# 卡片图输出（优先图片渲染器，异常回退纯文本）
# ---------------------------------------------------------------------------


def _renderer_module():
    """获取图片渲染器模块（未安装或未加载时返回 None）"""
    mod = sys.modules.get("plugin_image_renderer")
    if mod is not None and hasattr(mod, "_render_card_image"):
        return mod
    return None


async def _send_card(event, title, lines, width=560):
    """
    把若干行文本渲染成信息卡片图发送。
    成功返回 True；渲染器缺失 / 渲染异常 / 发送异常一律返回 False，
    由调用方回退纯文本回复。
    """
    mod = _renderer_module()
    if mod is None:
        return False
    content = "\n".join(str(x) for x in lines)
    try:
        # 渲染属 CPU 密集操作，丢进线程池执行（await 桥接线程池 Future）
        result = await asyncio.wrap_future(
            ctx.run_async(mod._render_card_image, title, content, width)
        )
    except Exception as e:
        ctx.log(f"卡片渲染异常，回退文本: {e}", level="warning")
        return False
    if result is None:
        return False

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        if isinstance(result, (bytes, bytearray)):
            with open(tmp_path, "wb") as f:
                f.write(bytes(result))
        else:
            # PIL Image 对象
            result.save(tmp_path, "PNG")
        # Windows 临时路径的反斜杠需转成正斜杠才能被 OneBot 客户端解析
        path_str = tmp_path.replace("\\", "/")
        await ctx.asend_msg(
            user_id=event.user_id,
            group_id=event.group_id if getattr(event, "is_group", False) else None,
            message=f"[CQ:image,file=file:///{path_str}]",
        )
        return True
    except Exception as e:
        ctx.log(f"卡片图发送失败: {e}", level="warning")
        return False
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


async def _reply_card_or_text(event, title, lines, width=560):
    """配置开启且渲染成功时发卡片图，否则回退纯文本（内容一致）"""
    if _cfg_bool("rank_card_enable", True) and await _send_card(event, title, lines, width):
        return
    await _reply(event, "\n".join(str(x) for x in lines))


# ---------------------------------------------------------------------------
# 消息流水记录（on_raw，不接管消息）
# ---------------------------------------------------------------------------


def on_raw(raw: dict, bot_name: str) -> bool:
    """记录入站消息纯文本到流水表；始终返回 False，不接管消息"""
    try:
        if not _cfg_bool("log_enable", True):
            return False
        if raw.get("post_type") != "message":
            return False
        uid = raw.get("user_id")
        gid = raw.get("group_id") or 0
        if not uid:
            return False
        content = _text_of(raw)[: _cfg_int("log_max_len", 2000)]
        ctx.db_execute(
            "INSERT INTO user_log (group_id, user_id, content, created_at) VALUES (%s,%s,%s,%s)",
            [gid, uid, content, int(time.time())],
        )
    except Exception:
        # 流水记录失败不影响消息正常流转
        pass
    return False


# ---------------------------------------------------------------------------
# 命令：发言数 / 排行 / 统计
# ---------------------------------------------------------------------------


async def handle_my_count(event, match):
    """/我的发言数 —— 查询自己累计发言条数"""
    try:
        rows = ctx.db_query(
            "SELECT COUNT(*) AS c FROM user_log WHERE user_id=%s", [event.user_id]
        )
        c = rows[0]["c"] if rows else 0
    except Exception as e:
        await _reply(event, f"查询失败: {e}")
        return
    await _reply(event, f"你累计发言 {c} 条（已被记录）")


async def handle_rank(event, match):
    """/群发言排行 —— 当前群发言量 Top N（卡片图优先）"""
    if not getattr(event, "is_group", False):
        await _reply(event, "请在群聊中使用")
        return
    gid = event.group_id
    top_n = max(1, min(50, _cfg_int("rank_top_n", 10)))
    try:
        rows = ctx.db_query(
            "SELECT user_id, COUNT(*) AS c FROM user_log WHERE group_id=%s "
            "GROUP BY user_id ORDER BY c DESC LIMIT %s",
            [gid, top_n],
        )
    except Exception as e:
        await _reply(event, f"查询失败: {e}")
        return
    if not rows:
        await _reply(event, "暂无发言记录")
        return

    lines = []
    for i, r in enumerate(rows, 1):
        uid = int(r["user_id"])
        lines.append(f"{i}. {_display_name(gid, uid)} — {r['c']} 条")

    title = f"🏆 群发言排行 Top{len(rows)}"
    card_lines = [f"群 {gid}", ""] + lines
    await _reply_card_or_text(event, title, card_lines)


async def handle_stat(event, match):
    """/发言统计 [群号] —— 群累计消息数与发言人数（卡片图优先）"""
    text = _match_arg(match)
    gid = _extract_number(text) or (event.group_id or 0)
    if not gid:
        await _reply(event, "用法: /发言统计 [群号]（群内使用可省略群号）")
        return
    try:
        rows = ctx.db_query(
            "SELECT COUNT(*) AS c, COUNT(DISTINCT user_id) AS u FROM user_log WHERE group_id=%s",
            [gid],
        )
        c = rows[0]["c"] if rows else 0
        u = rows[0]["u"] if rows else 0
    except Exception as e:
        await _reply(event, f"查询失败: {e}")
        return

    title = "📊 发言统计"
    lines = [
        f"群 {gid}",
        f"累计消息：{c} 条",
        f"发言人数：{u} 位",
    ]
    await _reply_card_or_text(event, title, lines)


# ---------------------------------------------------------------------------
# 命令：成员索引（同步 / 反查 / 状态）
# ---------------------------------------------------------------------------


async def handle_sync(event, match):
    """/同步群成员 [群号] —— 拉取群成员列表写入索引表"""
    text = _match_arg(match)
    gid = _extract_number(text)
    if not gid and getattr(event, "is_group", False):
        gid = event.group_id
    if not gid:
        await _reply(event, "用法: /同步群成员 [群号]")
        return
    try:
        members = ctx.get_member_list(gid) or []
    except Exception as e:
        await _reply(event, f"获取成员失败: {e}")
        return

    cnt = 0
    for mem in members:
        uid = mem.get("user_id")
        if not uid:
            continue
        nickname = _member_card(mem)
        role = mem.get("role", "member")
        try:
            # MySQL 方言：主键冲突时更新昵称与身份
            ctx.db_execute(
                "INSERT INTO user_index (group_id, user_id, nickname, role) VALUES (%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE nickname=%s, role=%s",
                [gid, uid, nickname, role, nickname, role],
            )
        except Exception:
            try:
                # SQLite 方言回退
                ctx.db_execute(
                    "INSERT OR REPLACE INTO user_index (group_id, user_id, nickname, role) VALUES (%s,%s,%s,%s)",
                    [gid, uid, nickname, role],
                )
            except Exception:
                continue
        cnt += 1
    await _reply(event, f"已同步群 {gid} 成员 {cnt} 人")


async def handle_find_user(event, match):
    """/查用户 <QQ> —— 反查该用户出现在哪些已同步的群"""
    uid = _extract_number(_match_arg(match))
    if not uid:
        await _reply(event, "用法: /查用户 <QQ号>")
        return
    try:
        rows = ctx.db_query(
            "SELECT group_id, nickname, role FROM user_index WHERE user_id=%s", [uid]
        )
    except Exception as e:
        await _reply(event, f"查询失败: {e}")
        return
    if not rows:
        await _reply(event, f"索引中未找到用户 {uid}")
        return
    lines = [f"🔍 {uid} 出现在 {len(rows)} 个群："]
    for r in rows:
        role = _ROLE_CN.get(r["role"], r["role"])
        lines.append(f"  群 {r['group_id']}（{r['nickname']}/{role}）")
    await _reply(event, "\n".join(lines))


async def handle_sync_status(event, match):
    """/同步状态 —— 已同步群数量与索引用户总量"""
    try:
        g = ctx.db_query("SELECT COUNT(DISTINCT group_id) AS c FROM user_index")
        u = ctx.db_query("SELECT COUNT(DISTINCT user_id) AS c FROM user_index")
        gc = g[0]["c"] if g else 0
        uc = u[0]["c"] if u else 0
    except Exception as e:
        await _reply(event, f"查询失败: {e}")
        return
    await _reply(event, f"已同步 {gc} 个群，索引 {uc} 名用户")


# ---------------------------------------------------------------------------
# 命令：成员查询（实时接口）
# ---------------------------------------------------------------------------


async def handle_search_member(event, match):
    """/查成员 <昵称片段> —— 当前群按昵称/名片模糊搜索（最多 10 条）"""
    if not getattr(event, "is_group", False):
        await _reply(event, "请在群聊中使用")
        return
    kw = _match_arg(match)
    if not kw:
        await _reply(event, "用法: /查成员 <昵称片段>")
        return
    try:
        members = ctx.get_member_list(event.group_id) or []
    except Exception as e:
        await _reply(event, f"获取成员列表失败: {e}")
        return
    hits = [m for m in members if kw.lower() in _member_card(m).lower()][:10]
    if not hits:
        await _reply(event, f"未找到昵称包含「{kw}」的成员")
        return
    lines = [f"🔍 匹配「{kw}」（{len(hits)} 条）："]
    for m in hits:
        lines.append(f"  {_member_card(m)} ({m.get('user_id')})")
    await _reply(event, "\n".join(lines))


async def handle_member_info(event, match):
    """/成员 <QQ> —— 查询某成员在本群的资料"""
    if not getattr(event, "is_group", False):
        await _reply(event, "请在群聊中使用")
        return
    uid = _extract_number(_match_arg(match))
    if not uid:
        await _reply(event, "用法: /成员 <QQ号>")
        return
    try:
        info = ctx.get_member_info(event.group_id, uid)
    except Exception as e:
        await _reply(event, f"查询失败: {e}")
        return
    if not info:
        await _reply(event, f"未找到成员 {uid}")
        return
    role = _ROLE_CN.get(info.get("role"), info.get("role"))
    join_ts = info.get("join_time")
    join_txt = _fmt_ts(join_ts) if isinstance(join_ts, (int, float)) else str(join_ts or "?")
    lines = [
        "👤 成员资料：",
        f"昵称：{_member_card(info)}",
        f"QQ：{info.get('user_id')}",
        f"群名片：{info.get('card') or '无'}",
        f"身份：{role}",
        f"群等级：{info.get('level', '?')}",
        f"加群时间：{join_txt}",
    ]
    await _reply(event, "\n".join(lines))


async def handle_member_count(event, match):
    """/群成员数 —— 当前群成员总数"""
    if not getattr(event, "is_group", False):
        await _reply(event, "请在群聊中使用")
        return
    try:
        members = ctx.get_member_list(event.group_id) or []
    except Exception as e:
        await _reply(event, f"获取失败: {e}")
        return
    await _reply(event, f"本群当前共有 {len(members)} 名成员")


# ---------------------------------------------------------------------------
# 命令：CSV 导出
# ---------------------------------------------------------------------------


async def handle_export_csv(event, match):
    """/导出群成员 —— 导出本群成员列表 CSV（昵称/名片/角色/入群时间）"""
    if not getattr(event, "is_group", False):
        await ctx.asend_msg(user_id=event.user_id, message="❌ 请在群内使用")
        return
    try:
        members = ctx.get_member_list(event.group_id) or []
    except Exception as e:
        await ctx.asend_msg(
            user_id=event.user_id, group_id=event.group_id,
            message=f"获取成员列表失败: {e}",
        )
        return
    if not members:
        await ctx.asend_msg(
            user_id=event.user_id, group_id=event.group_id,
            message="未获取到成员列表（可能权限不足或接口失败）",
        )
        return

    out_dir = os.path.join(ctx.get_data_dir(), "exports")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(
        out_dir, f"group_{event.group_id}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    )
    # utf-8-sig 带 BOM，方便 Excel 直接打开不乱码
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["QQ号", "昵称", "群名片", "角色", "入群时间", "等级"])
        for m in members:
            join_ts = m.get("join_time") or 0
            w.writerow([
                m.get("user_id", ""),
                m.get("nickname", ""),
                m.get("card") or m.get("nickname", ""),
                m.get("role", "member"),
                _fmt_ts(join_ts),
                m.get("level", ""),
            ])

    roles = {}
    for m in members:
        roles[m.get("role", "member")] = roles.get(m.get("role", "member"), 0) + 1
    stat = "，".join(
        f"{_ROLE_CN.get(k, k)} {v}" for k, v in sorted(roles.items())
    )
    await ctx.asend_msg(
        user_id=event.user_id, group_id=event.group_id,
        message=f"✅ 已导出 {len(members)} 名成员（{stat}）\n文件：{os.path.basename(path)}",
    )


# ---------------------------------------------------------------------------
# 每日清理任务（流水按天数/条数上限，导出文件按天数）
# ---------------------------------------------------------------------------


def _cleanup_exports(keep_days):
    """清理超过保留天数的导出 CSV 文件"""
    out_dir = os.path.join(ctx.get_data_dir(), "exports")
    if not os.path.isdir(out_dir):
        return
    cutoff = time.time() - keep_days * 86400
    removed = 0
    try:
        for name in os.listdir(out_dir):
            fp = os.path.join(out_dir, name)
            if not os.path.isfile(fp):
                continue
            try:
                if os.path.getmtime(fp) < cutoff:
                    os.unlink(fp)
                    removed += 1
            except Exception:
                continue
    except Exception as e:
        ctx.log(f"清理导出目录失败: {e}", level="warning")
        return
    if removed:
        ctx.log(f"已清理 {removed} 个过期导出文件")


def task_daily_cleanup():
    """每日清理：过期流水、超量流水、过期导出文件"""
    try:
        # 1) 按留存天数清理消息流水（0 = 永久保留）
        days = _cfg_int("retention_days", 90)
        if days > 0:
            cutoff = int(time.time()) - days * 86400
            deleted = ctx.db_execute(
                "DELETE FROM user_log WHERE created_at < %s", [cutoff]
            )
            if deleted:
                ctx.log(f"已清理 {days} 天前的消息流水 {deleted} 条")

        # 2) 按最大条数截断流水（保留最新 N 条，0 = 不限制）
        max_records = _cfg_int("log_max_records", 0)
        if max_records > 0:
            row = ctx.db_query_one(
                "SELECT id FROM user_log ORDER BY id DESC LIMIT 1 OFFSET %s",
                [max_records - 1],
            )
            if row and row.get("id") is not None:
                deleted = ctx.db_execute(
                    "DELETE FROM user_log WHERE id < %s", [row["id"]]
                )
                if deleted:
                    ctx.log(f"流水超出 {max_records} 条上限，已清理最旧 {deleted} 条")

        # 3) 清理过期导出文件（0 = 永久保留）
        keep_days = _cfg_int("export_keep_days", 7)
        if keep_days > 0:
            _cleanup_exports(keep_days)
    except Exception as e:
        ctx.log(f"每日清理任务异常: {e}", level="error")


# ---------------------------------------------------------------------------
# 注册入口
# ---------------------------------------------------------------------------


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.create_table(_SQL_CREATE_MSG)
    ctx.create_table(_SQL_CREATE_INDEX)
    _refresh_conf()

    # 消息流水记录（不接管消息，返回 False）
    ctx.on_raw_message(on_raw)

    # 发言数 / 排行 / 统计
    ctx.command("^/我的发言数\\s*$", handle_my_count, priority=60,
                description="查询自己累计发言条数")
    ctx.command("^/群发言排行\\s*$", handle_rank, priority=60, alias="/发言排行",
                description="当前群发言量排行（卡片图，回退文本）")
    ctx.command("^/发言统计\\s*([\\s\\S]*)$", handle_stat, priority=60,
                description="群发言统计，用法: /发言统计 [群号]（卡片图，回退文本）")

    # 成员索引
    ctx.command("^/同步群成员\\s*([\\s\\S]*)$", handle_sync, priority=60,
                description="拉取群成员列表写入索引，用法: /同步群成员 [群号]",
                require_admin=True)
    ctx.command("^/查用户\\s+([\\s\\S]+)", handle_find_user, priority=60,
                description="按 QQ 反查所在群，用法: /查用户 <QQ号>")
    ctx.command("^/同步状态\\s*$", handle_sync_status, priority=60,
                description="查看已同步群数与索引用户数")

    # 成员查询
    ctx.command("^/查成员\\s+([\\s\\S]+)", handle_search_member, priority=60,
                description="按昵称片段搜索本群成员，用法: /查成员 <昵称片段>")
    ctx.command("^/成员\\s+([\\s\\S]+)", handle_member_info, priority=60,
                description="查询成员资料，用法: /成员 <QQ号>")
    ctx.command("^/群成员数\\s*$", handle_member_count, priority=60,
                description="查看本群成员总数")

    # CSV 导出
    ctx.command("/导出群成员", handle_export_csv, priority=60,
                description="导出本群成员列表为 CSV（需管理员）",
                require_admin=True)

    # 每日清理任务（cron 可在配置中调整，热加载后生效）
    cron = str(_cfg("cleanup_cron", "30 4 * * *")).strip() or "30 4 * * *"
    ctx.task(cron, task_daily_cleanup, description="每日清理过期流水与导出文件")

    ctx.logger.info("成员数据中枢注册完成：消息流水 / 成员索引 / 成员查询 / CSV 导出已就绪")

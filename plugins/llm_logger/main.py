"""LLM 日志：订阅 llm.chat 事件，把每次对话的统计落地为日志，并提供查询命令"""
import json
import os
import threading
import time
from datetime import datetime

__plugin_meta__ = {
    "name": "LLM日志",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "记录每次 LLM 对话的次数/耗时/工具调用统计，支持 /对话日志 与 /对话统计 查询",
    "priority": 80,
}

ctx = None
_LOG_FILE = None
_lock = threading.Lock()
_count = -1            # 内存中的记录条数缓存，-1 表示尚未统计
_TRUNCATE_BATCH = 200  # 超限后攒一批再截断，避免每次追加都重写整个文件


def _cfg(key, default):
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


def _now_str(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _count_lines():
    try:
        with open(_LOG_FILE, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except FileNotFoundError:
        return 0
    except Exception as e:
        ctx.log(f"统计日志行数失败: {e}", level="warning")
        return 0


def _truncate(keep):
    """重写日志文件，仅保留最近 keep 条；返回截断后的条数"""
    tmp = _LOG_FILE + ".tmp"
    try:
        with open(_LOG_FILE, "r", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        kept = lines[-keep:]
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp, _LOG_FILE)
        return len(kept)
    except Exception as e:
        ctx.log(f"日志截断失败: {e}", level="warning")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return keep


def _append(record):
    """加锁追加一条记录；超过保留上限时自动截断最旧记录"""
    global _count
    with _lock:
        if _count < 0:
            _count = _count_lines()
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            _count += 1
        except Exception as e:
            ctx.log(f"日志写入失败: {e}", level="warning")
            return
        try:
            max_records = int(_cfg("max_records", 5000) or 0)
        except (TypeError, ValueError):
            max_records = 5000
        if max_records > 0 and _count >= max_records + _TRUNCATE_BATCH:
            _count = _truncate(max_records)


def _current_source():
    """尽力取当前对话的接入端来源（bot 实例名），取不到就不记录"""
    try:
        from framework.runtime import current_source_var
        return str(current_source_var.get() or "")
    except Exception:
        return ""


def on_llm_chat(payload):
    """llm.chat 事件回调（事件总线以 handler(payload) 形式调用，只传 payload）。

    payload 字段：ok / turns / elapsed_ms / tools(工具名列表) / chars / model。
    事件里不含用户/群信息，能拿多少记多少，不做补全猜测。
    """
    try:
        if not isinstance(payload, dict):
            return
        if not payload.get("ok", False) and not _cfg("log_failed", True):
            return
        ts = time.time()
        tools = payload.get("tools") or []
        record = {
            "ts": round(ts, 3),
            "time": _now_str(ts),
            "source": _current_source(),
            "ok": bool(payload.get("ok", False)),
            "turns": int(payload.get("turns") or 0),
            "elapsed_ms": int(payload.get("elapsed_ms") or 0),
            "tools": [str(t) for t in tools if t],
            "chars": int(payload.get("chars") or 0),
            "model": str(payload.get("model") or ""),
        }
        _append(record)
    except Exception as e:
        ctx.log(f"记录对话日志失败: {e}", level="warning")


def _load_records():
    """读取全部记录（按文件顺序，从旧到新）；坏行跳过"""
    records = []
    try:
        with open(_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
    except FileNotFoundError:
        pass
    except Exception as e:
        ctx.log(f"日志读取失败: {e}", level="warning")
    return records


def _store_stats():
    """读取对话子系统的会话存量（sessions/capacity/estimated_tokens），不可用时返回 None"""
    try:
        svc = ctx._framework.services.get("llm_core")
        if svc is None or not hasattr(svc, "store"):
            return None
        st = svc.store.stats() or {}
        return (f"{st.get('sessions', 0)} 个（容量 {st.get('capacity', 0)}，"
                f"约 {st.get('estimated_tokens', 0)} tokens）")
    except Exception:
        return None


async def handle_log(event, match=None):
    """查看最近的对话概览：/对话日志 [条数]"""
    try:
        n = int(_cfg("overview_default", 10) or 10)
    except (TypeError, ValueError):
        n = 10
    try:
        parts = str(event.message or "").split()
        if len(parts) >= 2:
            n = int(parts[1])
    except (TypeError, ValueError):
        pass
    n = max(1, min(n, 100))

    records = _load_records()
    if not records:
        await ctx.asend_msg(user_id=event.user_id,
                            group_id=event.group_id if event.is_group else None,
                            message="📜 暂无对话记录")
        return
    recent = records[-n:]
    lines = [f"📜 最近对话概览（{len(recent)}/{len(records)} 条）"]
    for rec in recent:
        mark = "✅" if rec.get("ok") else "❌"
        tool_names = rec.get("tools") or []
        tool_txt = "+".join(tool_names) if tool_names else "无工具"
        lines.append(
            f"{rec.get('time', '')} {mark} "
            f"{rec.get('turns', 0)}轮 {rec.get('elapsed_ms', 0) / 1000:.1f}s "
            f"{rec.get('chars', 0)}字 {rec.get('model', '')} [{tool_txt}]")
    await ctx.asend_msg(user_id=event.user_id,
                        group_id=event.group_id if event.is_group else None,
                        message="\n".join(lines))


async def handle_stats(event, match=None):
    """查看今日对话统计：/对话统计"""
    records = _load_records()
    today = datetime.now().strftime("%Y-%m-%d")
    today_recs = [r for r in records if str(r.get("time", "")).startswith(today)]
    total = len(today_recs)
    ok_count = sum(1 for r in today_recs if r.get("ok"))
    avg_ms = (sum(int(r.get("elapsed_ms") or 0) for r in today_recs) / total) if total else 0
    tool_counter = {}
    for r in today_recs:
        for t in (r.get("tools") or []):
            tool_counter[t] = tool_counter.get(t, 0) + 1
    tool_total = sum(tool_counter.values())

    lines = [f"📊 今日对话统计（{today}）",
             f"对话次数：{total}（累计 {len(records)} 条）"]
    if total:
        lines.append(f"成功率：{ok_count * 100.0 / total:.0f}%（{ok_count}/{total}）")
        lines.append(f"平均耗时：{avg_ms / 1000:.1f}s")
    else:
        lines.append("成功率：-（今日暂无对话）")
    lines.append(f"工具调用：{tool_total} 次")
    if tool_counter:
        top = sorted(tool_counter.items(), key=lambda kv: -kv[1])[:3]
        lines.append("常用工具：" + "、".join(f"{name}×{cnt}" for name, cnt in top))
    store_txt = _store_stats()
    if store_txt:
        lines.append(f"会话存量：{store_txt}")
    await ctx.asend_msg(user_id=event.user_id,
                        group_id=event.group_id if event.is_group else None,
                        message="\n".join(lines))


def register(plugin_ctx):
    global ctx, _LOG_FILE
    ctx = plugin_ctx
    _LOG_FILE = os.path.join(ctx.get_data_dir(), "chat_log.jsonl")
    ctx.on("llm.chat", on_llm_chat)
    ctx.command("/对话日志", handle_log, priority=50, alias="/llmlog",
                description="查看最近的 LLM 对话概览")
    ctx.command("/对话统计", handle_stats, priority=50, alias="/llmstats",
                description="查看今日 LLM 对话统计")
    ctx.log("LLM日志已注册：/对话日志 /对话统计")

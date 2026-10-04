"""
万能工具插件 (allinone)
========================
  /b64 <编码|解码> <文本>  -> Base64 编解码
  /时间戳 [时间戳]          -> 时间戳与日期互转（无参=当前时间）
  /随机数 [min] [max]       -> 生成随机整数
  /hash <文本> [算法]       -> md5/sha1/sha256
  /help 工具               -> 说明
纯本地计算，无外部依赖。
"""
import re
import base64
import hashlib
import random
import time
from datetime import datetime, timezone, timedelta

__plugin_meta__ = {
    "name": "万能工具箱",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "Base64/时间戳/随机数/哈希等本地小工具集合",
    "priority": 50,
}

ctx = None

_TZ = timezone(timedelta(hours=8))


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def handle_b64(event, match):
    text = (match.group(1) if match else "").strip()
    m = re.match(r"(编码|解码|encode|decode)\s+([\s\S]+)", text, re.I)
    if not m:
        await _reply(event, "用法: /b64 <编码|解码> <文本>")
        return
    mode, payload = m.group(1).lower(), m.group(2)
    try:
        if mode.startswith("编码") or mode == "encode":
            out = base64.b64encode(payload.encode("utf-8")).decode()
        else:
            out = base64.b64decode(payload.strip()).decode("utf-8")
        await _reply(event, out)
    except Exception as e:
        await _reply(event, f"Base64 处理失败: {e}")


async def handle_ts(event, match):
    text = (match.group(1) if match else "").strip()
    if not text:
        now = int(time.time())
        await _reply(event, f"当前时间戳：{now}\n对应：{datetime.fromtimestamp(now, _TZ).strftime('%Y-%m-%d %H:%M:%S')}")
        return
    try:
        ts = int(text)
        await _reply(event, f"{ts} → {datetime.fromtimestamp(ts, _TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    except ValueError:
        # 尝试日期转时间戳
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            await _reply(event, f"{text} → {int(dt.timestamp())}")
        except Exception:
            await _reply(event, "用法: /时间戳 [10位时间戳 或 YYYY-MM-DD HH:MM:SS]")


async def handle_rand(event, match):
    text = (match.group(1) if match else "").strip()
    parts = text.split()
    lo, hi = 1, 100
    if len(parts) >= 2:
        try:
            lo, hi = int(parts[0]), int(parts[1])
        except ValueError:
            await _reply(event, "用法: /随机数 [最小值] [最大值]")
            return
    elif len(parts) == 1:
        try:
            hi = int(parts[0])
        except ValueError:
            await _reply(event, "用法: /随机数 [最小值] [最大值]")
            return
    if lo > hi:
        lo, hi = hi, lo
    await _reply(event, f"🎲 随机数：{random.randint(lo, hi)}（范围 {lo}~{hi}）")


async def handle_hash(event, match):
    text = (match.group(1) if match else "").strip()
    m = re.match(r"(\S+)\s*(md5|sha1|sha256)?", text, re.I)
    if not m or not m.group(1):
        await _reply(event, "用法: /hash <文本> [md5|sha1|sha256]")
        return
    payload = m.group(1)
    alg = (m.group(2) or "md5").lower()
    try:
        h = hashlib.new(alg)
        h.update(payload.encode("utf-8"))
        await _reply(event, f"{alg.upper()}: {h.hexdigest()}")
    except Exception as e:
        await _reply(event, f"哈希失败: {e}")


async def handle_help(event, match):
    await _reply(event,
        "🧰 万能工具箱\n"
        "▸ /b64 <编码|解码> <文本>\n"
        "▸ /时间戳 [时间戳或日期]\n"
        "▸ /随机数 [min] [max]\n"
        "▸ /hash <文本> [md5|sha1|sha256]")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/b64\\s+([\\s\\S]+)", handle_b64, priority=50, description="Base64 编解码")
    ctx.command("/时间戳\\s*([\\s\\S]*)$", handle_ts, priority=50, description="时间戳转换")
    ctx.command("/随机数\\s*([\\s\\S]*)$", handle_rand, priority=50, description="随机数")
    ctx.command("/hash\\s+([\\s\\S]+)", handle_hash, priority=50, description="哈希计算")
    ctx.command("/help\\s+工具\\s*$", handle_help, priority=50, description="工具箱帮助")
    ctx.logger.info("万能工具箱插件注册完成")

"""
B站视频解析插件 (bv)
=====================
功能（从 AstrBot「bv」类插件迁移，功能逻辑重写）：
  /bv <BV号>   -> 解析 B 站视频：标题、UP主、时长、播放/弹幕/点赞、简介、封面
  /av <AV号>   -> 解析 av 号（别名）
数据来源：B 站公开 Web 接口 api.bilibili.com/x/web-interface/view。
"""
import re

import httpx

__plugin_meta__ = {
    "name": "B站视频解析",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "解析 B 站视频 BV/AV 号，返回标题/UP/数据/封面",
    "priority": 50,
}

ctx = None

_API = "https://api.bilibili.com/x/web-interface/view"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.bilibili.com",
}


def _fmt_dur(sec):
    try:
        sec = int(sec)
    except Exception:
        return "?"
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _fmt_num(n):
    try:
        n = int(n)
    except Exception:
        return str(n)
    if n >= 10000:
        return f"{n/10000:.1f}万"
    return str(n)


async def _reply(event, msg, group_id=None):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=group_id if group_id else (event.group_id if event.is_group else None),
        message=msg,
    )


async def handle_bv(event, match):
    """bv <BV/AV>"""
    text = (match.group(1) if match else "").strip()
    bv = re.search(r"(BV[0-9A-Za-z]+|av\d+)", text, re.I)
    if not bv:
        await _reply(event, "用法: /bv <BV号或AV号>，如 /bv BV1xx411c7mD")
        return
    code = bv.group(1)
    params = {"bvid": code} if code.lower().startswith("bv") else {"aid": code[2:]}
    try:
        async with httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
            r = await client.get(_API, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        await _reply(event, f"解析失败（网络/接口异常）: {e}")
        return
    if data.get("code") != 0 or not data.get("data"):
        await _reply(event, f"未找到该视频（{data.get('message', '未知错误')}）")
        return
    d = data["data"]
    stat = d.get("stat", {})
    gid = event.group_id if getattr(event, "is_group", False) else None
    # 封面图 + 文本（图片与文本分两条或合并）
    msg_parts = [
        f"📺 {d.get('title', '?')}",
        f"UP：{d.get('owner', {}).get('name', '?')}  时长：{_fmt_dur(d.get('duration'))}",
        f"播放：{_fmt_num(stat.get('view'))}  弹幕：{_fmt_num(stat.get('danmaku'))}  "
        f"点赞：{_fmt_num(stat.get('like'))}  投币：{_fmt_num(stat.get('coin'))}",
        f"链接：https://www.bilibili.com/video/{d.get('bvid')}",
    ]
    desc = (d.get("desc") or "").strip().replace("\n", " ")
    if desc:
        msg_parts.append("简介：" + desc[:120] + ("…" if len(desc) > 120 else ""))
    pic = d.get("pic")
    try:
        if pic:
            await ctx.asend_msg(user_id=event.user_id, group_id=gid,
                                message=f"[CQ:image,file={pic}]")
        await ctx.asend_msg(user_id=event.user_id, group_id=gid, message="\n".join(msg_parts))
    except Exception as e:
        await _reply(event, "\n".join(msg_parts))


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/bv\\s+([\\s\\S]+)", handle_bv, priority=50, description="解析 B 站视频")
    ctx.command("/av\\s+([\\s\\S]+)", handle_bv, priority=50, description="解析 B 站 AV 号")
    ctx.logger.info("B站解析插件注册完成")

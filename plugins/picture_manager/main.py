"""
图片管理插件 (picture_manager)
==============================
功能（从 AstrBot「picture_manager」类插件迁移，功能逻辑重写）：
  - 自动记录你最近发过的图片（按用户），/存图 <名称> 将其保存到插件图库
  - 也支持 /存图 <图片URL> <名称> 直接保存网络图片
  - /取图 <名称>  -> 取出图片发送
  - /图列表       -> 列出图库
图片存储于插件数据目录 plugins_dat/picture_manager/。
"""
import os
import re
import time

import httpx

__plugin_meta__ = {
    "name": "图片管理",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "保存/取出图片的本地图库（自动记录最近图片）",
    "priority": 50,
}

ctx = None

_LAST_IMG = {}  # user_id -> 最近图片 URL
_CREATE = """
CREATE TABLE IF NOT EXISTS pic_store (
    name VARCHAR(128) PRIMARY KEY,
    file_path VARCHAR(512) NOT NULL,
    owner BIGINT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT 0
)
"""


def _data_dir():
    d = ctx.get_data_dir()
    os.makedirs(d, exist_ok=True)
    return d


def _img_url_of(raw):
    msg = raw.get("message", [])
    if isinstance(msg, list):
        for s in msg:
            if isinstance(s, dict) and s.get("type") == "image":
                data = s.get("data", {})
                return data.get("url") or data.get("file")
    return None


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def on_raw(raw: dict, bot_name: str) -> bool:
    if raw.get("post_type") == "message":
        uid = raw.get("user_id")
        url = _img_url_of(raw)
        if uid and url:
            _LAST_IMG[int(uid)] = str(url)
    return False


async def _download(url: str, dest: str):
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        with open(dest, "wb") as f:
            f.write(r.content)


async def handle_save(event, match):
    """存图 <名称|URL 名称>"""
    text = (match.group(1) if match else "").strip()
    parts = text.split(None, 1)
    if not parts:
        await _reply(event, "用法: /存图 <名称>  或  /存图 <图片URL> <名称>")
        return
    name = parts[-1]
    url = None
    if len(parts) == 2 and parts[0].startswith("http"):
        url = parts[0]
    else:
        url = _LAST_IMG.get(event.user_id)
    if not url:
        await _reply(event, "未找到要保存的图片：请先发送一张图片，或用 /存图 <URL> <名称>")
        return
    dest = os.path.join(_data_dir(), f"{int(time.time()*1000)}_{abs(hash(name))%100000}.png")
    try:
        await _download(url, dest)
    except Exception as e:
        await _reply(event, f"下载图片失败: {e}")
        return
    try:
        ctx.db_execute(
            "INSERT INTO pic_store (name, file_path, owner, created_at) VALUES (%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE file_path=%s, owner=%s, created_at=%s",
            [name, dest, event.user_id, int(time.time()), dest, event.user_id, int(time.time())],
        )
    except Exception:
        try:
            ctx.db_execute(
                "INSERT OR REPLACE INTO pic_store (name, file_path, owner, created_at) VALUES (%s,%s,%s,%s)",
                [name, dest, event.user_id, int(time.time())],
            )
        except Exception as e:
            await _reply(event, f"登记失败: {e}")
            return
    await _reply(event, f"已保存图片「{name}」")


async def handle_get(event, match):
    """取图 <名称>"""
    name = (match.group(1) if match else "").strip()
    if not name:
        await _reply(event, "用法: /取图 <名称>")
        return
    row = ctx.db_query_one("SELECT file_path FROM pic_store WHERE name=%s", [name])
    if not row:
        await _reply(event, f"图库中没有「{name}」")
        return
    path = row["file_path"].replace("\\", "/")
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=f"[CQ:image,file=file:///{path}]",
    )


async def handle_list(event, match):
    rows = ctx.db_query("SELECT name FROM pic_store ORDER BY created_at DESC LIMIT 30")
    if not rows:
        await _reply(event, "图库为空")
        return
    await _reply(event, "🖼 图库：" + "、".join(r["name"] for r in rows))


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.db_execute(_CREATE, [])
    ctx.on_raw_message(on_raw)
    ctx.command("/存图\\s+([\\s\\S]+)", handle_save, priority=50, description="保存图片到图库")
    ctx.command("/取图\\s+([\\s\\S]+)", handle_get, priority=50, description="取出图片")
    ctx.command("/图列表\\s*$", handle_list, priority=50, description="列出图库")
    ctx.logger.info("图片管理插件注册完成")

"""
点歌插件 (music)
=================
  /点歌 <歌名>   -> 搜索并以音乐卡片形式返回可播放链接
  /点歌帮助       -> 说明
数据来源：Meting 公共接口（代理网易云/QQ/酷狗），默认使用 QQ 源。
"""
import re

import httpx

__plugin_meta__ = {
    "name": "点歌",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "搜索歌曲并以音乐卡片返回可播放链接（Meting 公共接口）",
    "priority": 50,
}

ctx = None

_BASE = "https://api.injahow.cn/meting/"
_HEADERS = {"User-Agent": "Mozilla/5.0"}


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


def _source():
    try:
        return str(ctx.get_config("music_source", "tencent")).strip() or "tencent"
    except Exception:
        return "tencent"


async def handle_music(event, match):
    """点歌 <歌名>"""
    name = (match.group(1) if match else "").strip()
    if not name:
        await _reply(event, "用法: /点歌 <歌名或歌手>，如 /点歌 周杰伦 晴天")
        return
    src = _source()
    try:
        async with httpx.AsyncClient(timeout=25, headers=_HEADERS, follow_redirects=True) as client:
            sr = await client.get(_BASE, params={"type": "search", "source": src, "name": name})
            sr.raise_for_status()
            songs = sr.json()
            if not isinstance(songs, list) or not songs:
                await _reply(event, f"未搜到「{name}」相关歌曲")
                return
            top = songs[0]
            sid = top.get("id") or top.get("songid") or top.get("mid")
            if not sid:
                await _reply(event, "搜索结果为空")
                return
            ur = await client.get(_BASE, params={"type": "url", "source": src, "id": sid})
            ur.raise_for_status()
            urls = ur.json()
            url = urls[0].get("url") if isinstance(urls, list) and urls else ""
            if not url:
                await _reply(event, "获取播放链接失败")
                return
            title = top.get("name", name)
            artist = top.get("artist", "")
            pic = top.get("pic", "")
            # 发送自定义音乐卡片
            music_cq = (
                f"[CQ:music,type=custom,title={title},"
                f"content={artist},url={url},"
                f"audio={url},image={pic}]"
            )
            await ctx.asend_msg(
                user_id=event.user_id,
                group_id=event.group_id if event.is_group else None,
                message=music_cq,
            )
    except Exception as e:
        await _reply(event, f"点歌失败（接口/网络异常）: {e}")


async def handle_help(event, match):
    await _reply(event, "🎵 点歌插件\n▸ /点歌 <歌名或歌手>  → 返回可播放音乐卡片\n▸ /点歌帮助")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/点歌\\s+([\\s\\S]+)", handle_music, priority=50, description="点歌")
    ctx.command("/点歌帮助\\s*$", handle_help, priority=50, description="点歌帮助")
    ctx.logger.info("点歌插件注册完成")

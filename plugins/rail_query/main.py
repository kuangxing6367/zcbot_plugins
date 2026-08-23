"""
列车查询插件 (rail_query)
=========================
功能（从 AstrBot「railquery」类插件迁移，功能逻辑重写）：
  /列车 <车次>    -> 查询列车经停站、到发时间、历时
  /车次 <车次>    -> 同上（别名）
数据来源：oioweb 公共列车接口 api.oioweb.cn/api/train/query。
"""
import re

import httpx

__plugin_meta__ = {
    "name": "列车查询",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "按车次查询列车经停站、到发时间与历时",
    "priority": 50,
}

ctx = None

_API = "https://api.oioweb.cn/api/train/query"
_HEADERS = {"User-Agent": "Mozilla/5.0"}


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def handle_train(event, match):
    """列车 <车次>"""
    code = (match.group(1) if match else "").strip().upper()
    if not code:
        await _reply(event, "用法: /列车 <车次号>，如 /列车 G1")
        return
    try:
        async with httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
            r = await client.get(_API, params={"train": code})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        await _reply(event, f"查询失败（网络/接口异常）: {e}")
        return
    if data.get("code") != 200 or not data.get("data"):
        await _reply(event, f"未查到车次 {code}（{data.get('msg', '无数据')}）")
        return
    d = data["data"]
    lines = [f"🚄 车次 {code}（{d.get('trainType','')}）  历时 {d.get('runTime','?')}"]
    stops = d.get("data", [])
    if not stops:
        await _reply(event, f"车次 {code} 暂无经停信息")
        return
    lines.append(f"始发：{stops[0].get('stationName')} {stops[0].get('startTime')}")
    lines.append(f"终到：{stops[-1].get('stationName')} {stops[-1].get('arriveTime')}")
    lines.append("——— 经停 ———")
    for s in stops[:25]:
        lines.append(
            f"{s.get('stationName')}  到 {s.get('arriveTime','-')}  发 {s.get('startTime','-')}  "
            f"停{s.get('stopOver','-')}"
        )
    await _reply(event, "\n".join(lines))


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/列车\\s+([\\s\\S]+)", handle_train, priority=50, description="按车次查列车")
    ctx.command("/车次\\s+([\\s\\S]+)", handle_train, priority=50, description="按车次查列车（别名）")
    ctx.logger.info("列车查询插件注册完成")

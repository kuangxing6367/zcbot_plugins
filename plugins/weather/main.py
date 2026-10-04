"""
天气查询插件 (weather)
========================
  /天气 <城市>   -> 查询城市当前天气 + 未来 3 天预报
  /天气帮助      -> 说明
数据来源：wttr.in 公共接口（JSON 格式 j1，中文 lang=zh）。
"""
import re
from urllib.parse import quote

import httpx

__plugin_meta__ = {
    "name": "天气查询",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "基于 wttr.in 的城市天气查询（当前+3天预报）",
    "priority": 50,
}

ctx = None

_WTTRL_URL = "https://wttr.in/{city}"
_HEADERS = {"User-Agent": "curl/7.68.0", "Accept-Language": "zh-CN"}


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


def _fmt_day(day):
    d = day.get("date", "")
    maxc = day.get("maxtempC", "?")
    minc = day.get("mintempC", "?")
    desc = ""
    if day.get("hourly") and day["hourly"][0].get("weatherDesc"):
        desc = day["hourly"][0]["weatherDesc"][0].get("value", "")
    return f"  {d}  {desc}  {minc}°C~{maxc}°C"


async def handle_weather(event, match):
    """天气 <城市>"""
    city = (match.group(1) if match else "").strip()
    if not city:
        await _reply(event, "用法: /天气 <城市名>，如 /天气 北京")
        return
    try:
        async with httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
            r = await client.get(_WTTRL_URL.format(city=quote(city)), params={"format": "j1", "lang": "zh"})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        await _reply(event, f"查询失败（接口或网络异常）: {e}")
        return

    try:
        cur = data["current_condition"][0]
        area = data.get("nearest_area", [{}])[0].get("areaName", [{}])[0].get("value", city)
        lines = [
            f"🌤 {area} 天气",
            f"当前：{cur['weatherDesc'][0]['value']}  {cur['temp_C']}°C",
            f"体感：{cur['FeelsLikeC']}°C  湿度：{cur['humidity']}%  风：{cur['windspeedKmph']}km/h",
            "———",
            "未来三天：",
        ]
        for day in data.get("weather", [])[:3]:
            lines.append(_fmt_day(day))
        await _reply(event, "\n".join(lines))
    except Exception as e:
        await _reply(event, f"解析天气数据失败: {e}")


async def handle_help(event, match):
    await _reply(event, "🌦 天气插件\n▸ /天气 <城市名>  → 当前天气+3天预报\n▸ /天气帮助")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/天气\\s+([\\s\\S]+)", handle_weather, priority=50, description="查询城市天气")
    ctx.command("/天气帮助\\s*$", handle_help, priority=50, description="天气插件帮助")
    ctx.logger.info("天气插件注册完成")

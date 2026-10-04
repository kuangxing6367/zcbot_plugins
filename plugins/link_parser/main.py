"""
链接解析中枢 (link_parser)
===========================
群/私聊消息里出现链接时,按平台自动分发,一个入口处理全部链接类消息:

  1. B站(bilibili.com / b23.tv / BV号 / av号) → B站官方 Web 接口,
     返回标题/UP主/时长/互动数据/简介/封面,不依赖第三方解析服务
  2. 其他视频平台分享链接(抖音/快手/小红书/微博/西瓜/皮皮虾/梨视频/
     腾讯视频/YouTube) → 聚合解析接口(需配置 fengyu_api_key,未配置则关闭)
     返回标题/作者/平台/互动数据/封面,不含视频直链
  3. 其余 http(s) 链接 → 网页分析:标题/描述/风险关键词标注,
     群内自动分析按群开关(默认关)

命令:
  /bv <BV|AV号>    解析 B 站视频
  /解析 <链接>     手动解析视频链接
  /分析 <url>      手动分析网页
  分析 开|关        群内开关自动网页分析(管理员)
  /链接帮助        使用帮助

已注册为 LLM AI 函数: parse_video(解析视频分享链接,AI 可基于返回信息回答)。
"""
import json
import os
import re
import tempfile
import threading
import time
from urllib.parse import urlparse

import httpx

__plugin_meta__ = {
    "name": "链接解析",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "B站/视频分享/网页链接统一解析:自动识别平台分发,支持LLM函数",
    "priority": 45,
}

ctx = None
_llm_registered = False

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_BILI_API = "https://api.bilibili.com/x/web-interface/view"
_BILI_HEADERS = {"User-Agent": _UA, "Referer": "https://www.bilibili.com"}

# B站:视频页/短链/裸 BV|av 号
_BILI_CODE_RE = re.compile(r"\b(BV[0-9A-Za-z]{8,}|av\d+)\b", re.I)
_BILI_HOST_RE = re.compile(r"bilibili\.com|b23\.tv", re.I)

# 视频平台域名(命中走聚合解析;B站优先走官方接口,这里仅作兜底)
_VIDEO_HOSTS_RE = re.compile(
    r"douyin\.com|iesdouyin\.com|kuaishou\.com|bilibili\.com|b23\.tv|"
    r"xiaohongshu\.com|xhslink\.com|weibo\.com|ixigua\.com|pipix\.com|"
    r"pearvideo\.com|v\.qq\.com|youtube\.com|youtu\.be", re.I)

# 任意 http(s) 链接(用于网页分析)
_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.I)

PLATFORM_NAMES = {
    "dy": "抖音", "douyin": "抖音",
    "ks": "快手", "kuaishou": "快手",
    "bili": "B站", "bilibili": "B站", "哔哩哔哩": "B站",
    "xhs": "小红书", "xiaohongshu": "小红书",
    "wb": "微博", "weibo": "微博",
    "xg": "西瓜视频", "ixigua": "西瓜视频",
    "pipix": "皮皮虾",
    "pear": "梨视频",
    "qq": "腾讯视频", "vqq": "腾讯视频",
    "yt": "YouTube", "youtube": "YouTube",
}

# 网页风险标注
_RISK_DOMAINS = ("login", "account", "secure", "verify", "pay", "bank", "captcha")
_RISK_KW = ("中奖", "领取红包", "免费领", "点击领取", "验证码", "账户异常")

# 自动解析冷却:(group_id, url) -> 时间戳
_cooldown_map = {}
_COOLDOWN_MAP_MAX = 5000
_cooldown_lock = threading.Lock()


# ================= 配置 =================

def _cfg(key, default=None):
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


# ================= 链接提取与分类 =================

def _clean_url(u):
    return (u or "").strip().rstrip(".,;!?，。；！？)）】")


def extract_urls(text):
    """从文本提取全部 http(s) 链接(去重保序)"""
    seen, out = set(), []
    for m in _URL_RE.finditer(text or ""):
        u = _clean_url(m.group(0))
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def extract_urls_from_event(event):
    """文本 + 消息段(share/json 卡片)里的链接都收上来"""
    urls = extract_urls(getattr(event, "message", "") or "")
    for seg in getattr(event, "segments", None) or []:
        try:
            if not isinstance(seg, dict):
                continue
            seg_type, data = seg.get("type", ""), seg.get("data", {}) or {}
            if seg_type == "share" and data.get("url"):
                u = _clean_url(str(data["url"]))
                if u:
                    urls.append(u)
            elif seg_type == "json":
                urls.extend(extract_urls(str(data.get("data", ""))))
        except Exception:
            continue
    return list(dict.fromkeys(urls))


def _text_from_raw(raw):
    msg = raw.get("message", "")
    if isinstance(msg, list):
        return "".join(s.get("data", {}).get("text", "") for s in msg
                       if isinstance(s, dict) and s.get("type") == "text")
    return str(msg or "")


def _urls_from_raw(raw, text):
    """raw 事件里的全部链接:文本 + share/json 卡片"""
    urls = extract_urls(text)
    msg = raw.get("message", [])
    if isinstance(msg, list):
        for seg in msg:
            try:
                if not isinstance(seg, dict):
                    continue
                if seg.get("type") == "share" and seg.get("data", {}).get("url"):
                    urls.append(_clean_url(str(seg["data"]["url"])))
                elif seg.get("type") == "json":
                    urls.extend(extract_urls(str(seg.get("data", {}).get("data", ""))))
            except Exception:
                continue
    return list(dict.fromkeys(urls))


def classify(url):
    """返回 'bili' / 'video' / 'web'"""
    if _BILI_CODE_RE.search(url) or _BILI_HOST_RE.search(url):
        return "bili"
    if _VIDEO_HOSTS_RE.search(url):
        return "video"
    return "web"


# ================= B站(官方接口) =================

def _fmt_dur(sec):
    try:
        sec = int(sec)
    except Exception:
        return "?"
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_num(n):
    try:
        n = int(n)
    except Exception:
        return str(n)
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def _bili_params(code):
    if code.upper().startswith("BV"):
        return {"bvid": code}
    return {"aid": code[2:]}


def _bili_parse_payload(data):
    """接口回执 → (文字, 封面URL)"""
    d = data["data"]
    stat = d.get("stat", {})
    parts = [
        f"📺 {d.get('title', '?')}",
        f"UP:{d.get('owner', {}).get('name', '?')}  时长:{_fmt_dur(d.get('duration'))}",
        f"播放:{_fmt_num(stat.get('view'))}  弹幕:{_fmt_num(stat.get('danmaku'))}  "
        f"点赞:{_fmt_num(stat.get('like'))}  投币:{_fmt_num(stat.get('coin'))}",
        f"链接:https://www.bilibili.com/video/{d.get('bvid')}",
    ]
    desc = (d.get("desc") or "").strip().replace("\n", " ")
    if desc:
        parts.append("简介:" + desc[:120] + ("…" if len(desc) > 120 else ""))
    return "\n".join(parts), d.get("pic") or ""


async def bili_fetch(code):
    """B站官方接口(异步);返回 (data, None) 或 (None, 错误信息)"""
    try:
        async with httpx.AsyncClient(timeout=20, headers=_BILI_HEADERS,
                                     follow_redirects=True) as client:
            r = await client.get(_BILI_API, params=_bili_params(code))
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return None, f"网络/接口异常: {e}"
    if data.get("code") != 0 or not data.get("data"):
        return None, data.get("message", "未知错误")
    return data["data"], None


async def _resolve_bili_code(url):
    """从链接提取 BV|av 号;b23.tv 短链跟进重定向后提取"""
    m = _BILI_CODE_RE.search(url)
    if m:
        return m.group(1)
    if "b23.tv" in url:
        try:
            async with httpx.AsyncClient(timeout=15, headers=_BILI_HEADERS,
                                         follow_redirects=True) as client:
                r = await client.get(url)
            m = _BILI_CODE_RE.search(str(r.url))
            if m:
                return m.group(1)
        except Exception:
            pass
    return None


async def handle_bili(url=None, code=None, event=None):
    """B站解析分支;找不到视频号返回 False(交给聚合解析兜底)"""
    if not code and url:
        code = await _resolve_bili_code(url)
    if not code:
        return False
    d, err = await bili_fetch(code)
    if d is None:
        if event is not None:
            await _areply(event, f"B站解析失败({err})")
        return True
    text, pic = _bili_parse_payload(d)
    if pic and _cfg("send_cover", True) and event is not None:
        gid = event.group_id if getattr(event, "is_group", False) else None
        await ctx.asend_msg(user_id=event.user_id, group_id=gid,
                            message=f"[CQ:image,file={pic}]")
    await _areply(event, text)
    return True


async def handle_bv_cmd(event, match):
    """/bv /av <BV号|AV号|链接>"""
    text = (match.group(1) if match else "").strip()
    m = _BILI_CODE_RE.search(text)
    if not m:
        await _areply(event, "用法: /bv <BV号或AV号>,如 /bv BV1xx411c7mD")
        return
    await handle_bili(code=m.group(1), event=event)


# ================= 视频平台(聚合解析) =================

def _pick_count(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            try:
                return int(float(str(v).replace("万", "0000").replace("亿", "00000000")))
            except (TypeError, ValueError):
                return v
    return 0


def _parse_video_payload(data):
    """聚合接口回执 → 精简信息 dict(兼容各平台 author/title/count 结构差异)"""
    d = data.get("data") or {}
    author = d.get("author") or data.get("author") or {}
    raw_count = d.get("count")
    if not isinstance(raw_count, dict):
        raw_count = data.get("count")
    if not isinstance(raw_count, dict):
        raw_count = {}
    keys = ("like", "likes", "digg", "zan", "comment", "comments",
            "share", "shares", "collect", "favorite", "favorites")
    return {
        "title": (d.get("title") or d.get("desc") or "").strip(),
        "author": (author.get("name") or "").strip(),
        "platform": str(data.get("platform", "") or ""),
        "type": str(data.get("type", "") or ""),
        "cover": (d.get("cover") or "").strip(),
        "like": _pick_count(raw_count, "like", "likes", "digg", "zan"),
        "comment": _pick_count(raw_count, "comment", "comments"),
        "share": _pick_count(raw_count, "share", "shares"),
        "collect": _pick_count(raw_count, "collect", "favorite", "favorites"),
        "has_count": any(_pick_count(raw_count, k) for k in keys),
    }


def video_fetch_sync(url):
    """聚合解析(同步版,LLM 函数用);返回 (info, None) 或 (None, 'not_configured'|'error')"""
    api_url = _cfg("fengyu_api_url", "https://api-v2.yuafeng.cn/API/juhejx.php")
    api_key = (_cfg("fengyu_api_key", "") or "").strip()
    if not api_key:
        return None, "not_configured"
    try:
        timeout = int(_cfg("timeout", 10) or 10)
        with httpx.Client(timeout=timeout, headers={"User-Agent": _UA}) as client:
            r = client.get(api_url, params={"url": url, "apikey": api_key})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        ctx.log(f"[link_parser] 聚合解析请求失败: {e}", level="error")
        return None, "error"
    if data.get("code") != 0:
        ctx.log(f"[link_parser] 聚合解析返回错误: code={data.get('code')} "
                f"msg={data.get('msg')}", level="warning")
        return None, "error"
    return _parse_video_payload(data), None


async def video_fetch(url):
    """聚合解析(异步版,消息处理用)"""
    api_url = _cfg("fengyu_api_url", "https://api-v2.yuafeng.cn/API/juhejx.php")
    api_key = (_cfg("fengyu_api_key", "") or "").strip()
    if not api_key:
        return None, "not_configured"
    try:
        timeout = int(_cfg("timeout", 10) or 10)
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": _UA}) as client:
            r = await client.get(api_url, params={"url": url, "apikey": api_key})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        ctx.log(f"[link_parser] 聚合解析请求失败: {e}", level="error")
        return None, "error"
    if data.get("code") != 0:
        ctx.log(f"[link_parser] 聚合解析返回错误: code={data.get('code')} "
                f"msg={data.get('msg')}", level="warning")
        return None, "error"
    return _parse_video_payload(data), None


def _download_cover(url):
    try:
        with httpx.Client(timeout=8, headers={"User-Agent": _UA}) as client:
            r = client.get(url)
        if len(r.content) < 100:
            return None
        fd, path = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            f.write(r.content)
        return path
    except Exception as e:
        ctx.log(f"[link_parser] 封面下载失败: {e}", level="warning")
        return None


def _cleanup_later(path, delay=15):
    """延迟删除封面临时文件(等 OneBot 客户端读完)"""
    def _rm():
        try:
            os.unlink(path)
        except Exception:
            pass
    threading.Timer(delay, _rm).start()


async def video_text(info):
    plat = PLATFORM_NAMES.get(info["platform"].lower(), info["platform"] or "未知")
    max_len = int(_cfg("max_title_len", 100) or 100)
    lines = ["📹 视频解析"]
    head = f"平台:{plat}"
    if info.get("type"):
        head += f"|类型:{info['type']}"
    lines.append(head)
    if info.get("title"):
        title = info["title"]
        lines.append(f"标题:{title if len(title) <= max_len else title[:max_len] + '…'}")
    if info.get("author"):
        lines.append(f"作者:{info['author']}")
    if info.get("has_count"):
        lines.append(f"👍{_fmt_num(info['like'])} 💬{_fmt_num(info['comment'])} "
                     f"🔄{_fmt_num(info['share'])} ⭐{_fmt_num(info['collect'])}")
    return "\n".join(lines)


def _in_cooldown(event, url):
    cd = int(_cfg("cooldown_seconds", 30) or 0)
    if cd <= 0:
        return False
    gid = event.group_id if getattr(event, "is_group", False) else None
    key = (gid, url)
    now = time.time()
    with _cooldown_lock:
        last = _cooldown_map.get(key, 0)
        if now - last < cd:
            return True
        _cooldown_map[key] = now
        if len(_cooldown_map) > _COOLDOWN_MAP_MAX:
            expired = [k for k, t in _cooldown_map.items() if now - t >= cd]
            for k in expired:
                _cooldown_map.pop(k, None)
    return False


async def handle_video(url, event, auto=False):
    """聚合解析分支;not_configured 仅手动模式提示配置方法,自动模式静默"""
    if auto and _in_cooldown(event, url):
        return
    info, err = await video_fetch(url)
    if info is None:
        if not auto and event is not None:
            if err == "not_configured":
                await _areply(event, "视频解析未配置接口密钥,请在 Web 面板插件配置中填写 fengyu_api_key")
            else:
                await _areply(event, "视频解析失败,请稍后再试~(链接可能无效或接口繁忙)")
        return
    text = await video_text(info)
    if info.get("cover") and _cfg("send_cover", True) and event is not None:
        path = await ctx.run_async(_download_cover, info["cover"])
        if path:
            gid = event.group_id if getattr(event, "is_group", False) else None
            await ctx.asend_msg(user_id=event.user_id, group_id=gid,
                                message=f"[CQ:image,file=file:///{path}]")
            _cleanup_later(path)
    await _areply(event, text)


async def handle_parse_cmd(event, match):
    """/解析 <链接>"""
    urls = extract_urls_from_event(event)
    if not urls:
        await _areply(event, "用法: /解析 <视频分享链接>\n支持抖音/快手/B站/小红书/微博/西瓜/皮皮虾等")
        return
    url = urls[0]
    if not await handle_bili(url=url, event=event):
        await handle_video(url, event, auto=False)


# ================= 网页分析 =================

def _extract_meta(html):
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        title = m.group(1).strip()
    desc = ""
    m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
                  html, re.S | re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']',
                      html, re.S | re.I)
    if m:
        desc = m.group(1).strip()
    og = ""
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
                  html, re.S | re.I)
    if m:
        og = m.group(1).strip()
    return title, desc, og


def _risk_flags(html, host):
    flags = []
    for kw in _RISK_KW:
        if kw in (html or ""):
            flags.append(kw)
    for d in _RISK_DOMAINS:
        if d in (host or ""):
            flags.append(f"域名含'{d}'")
    return flags


async def web_analyze(url):
    """抓取网页出分析文本(标题/描述/风险标注)"""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": _UA}) as client:
            r = await client.get(url)
            html = r.text
        title, desc, og = _extract_meta(html)
        host = urlparse(str(r.url)).netloc
        lines = [
            f"🔗 网页分析:{host}",
            f"标题:{title or og or '(无)'}",
            f"描述:{(desc or '(无)')[:200]}",
            f"正文长度:{len(html)} 字符",
        ]
        flags = _risk_flags(html, host)
        lines.append("⚠️ 风险提示:" + "、".join(flags[:5]) if flags
                     else "✅ 未发现明显风险关键词")
        return "\n".join(lines)
    except Exception as e:
        return f"分析失败(网络/解析异常): {e}"


def _web_auto_enabled(gid):
    try:
        row = ctx.db_query_one(
            "SELECT config_value FROM plugin_configs WHERE plugin_name=%s AND config_key=%s",
            ["link_parser", f"web_auto_{gid}"])
        return str(row.get("config_value")) == "1" if row else False
    except Exception:
        return False


def _web_auto_set(gid, on):
    val = "1" if on else "0"
    try:
        ctx.db_execute(
            "INSERT INTO plugin_configs (plugin_name, config_key, config_value) "
            "VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE config_value=%s",
            ["link_parser", f"web_auto_{gid}", val, val])
    except Exception:
        ctx.db_execute(
            "INSERT OR REPLACE INTO plugin_configs (plugin_name, config_key, config_value) "
            "VALUES (%s,%s,%s)", ["link_parser", f"web_auto_{gid}", val])


async def handle_analyze_cmd(event, match):
    """/分析 <url>"""
    url = (match.group(1) if match else "").strip()
    if not url.startswith("http"):
        await _areply(event, "用法: /分析 <网页URL>")
        return
    await _areply(event, await web_analyze(url))


async def handle_web_toggle(event, match):
    """分析 开|关:群内自动网页分析开关(管理员)"""
    if not getattr(event, "is_group", False):
        await _areply(event, "请在群聊中使用")
        return
    on = (match.group(2) if match and match.lastindex and match.lastindex >= 2
          else "") == "开"
    try:
        _web_auto_set(event.group_id, on)
    except Exception as e:
        await _areply(event, f"设置失败: {e}")
        return
    await _areply(event, f"自动网页分析已{'开启' if on else '关闭'}")


# ================= 统一分发入口 =================

async def on_raw(raw: dict, bot_name: str) -> bool:
    """链接类消息唯一分发入口:B站→官方接口,视频平台→聚合解析,
    其余→群内自动网页分析(按群开关)。不接管消息,不影响其他插件。"""
    if raw.get("post_type") != "message":
        return False
    text = _text_from_raw(raw).strip()
    if not text or text.startswith(("/", "／")):
        return False
    # 被 @ 的消息让给 LLM 对话处理,不抢话
    if text.startswith("[@"):
        return False

    urls = _urls_from_raw(raw, text)
    if not urls:
        return False

    class _Ev:
        pass

    ev = _Ev()
    ev.user_id = raw.get("user_id")
    ev.group_id = raw.get("group_id")
    ev.is_group = raw.get("message_type") == "group"

    auto_video_ok = _cfg("auto_parse", True) and (
        (ev.is_group and _cfg("group_auto_parse", True))
        or ((not ev.is_group) and _cfg("private_auto_parse", True)))
    web_auto = ev.is_group and _web_auto_enabled(ev.group_id)

    for url in urls:
        kind = classify(url)
        try:
            if kind == "bili":
                # 短链重定向失败等取不到视频号时,交给聚合解析兜底
                if not await handle_bili(url=url, event=ev):
                    await handle_video(url, ev, auto=True)
            elif kind == "video":
                if auto_video_ok:
                    await handle_video(url, ev, auto=True)
            elif web_auto:
                await ctx.asend_msg(user_id=ev.user_id, group_id=ev.group_id,
                                    message=await web_analyze(url))
        except Exception as e:
            ctx.log(f"[link_parser] 链接处理异常({kind}): {e}", level="warning")
    return False


# ================= LLM AI 函数 =================

def _try_register_llm():
    global _llm_registered
    if _llm_registered:
        return
    try:
        svc = ctx._framework.services.get('llm_core')
        if svc is None:
            raise RuntimeError("llm_core 服务未加载")

        @svc.tool(name="parse_video",
                  description=("解析视频分享链接(抖音/快手/B站/小红书/微博/西瓜/皮皮虾等),"
                               "返回视频标题、作者、平台、互动数据与封面图地址。"
                               "用户发来视频链接或询问视频内容时调用。"))
        def parse_video(url: str, event=None):
            return _fn_parse_video({"url": url})

        _llm_registered = True
        ctx.log("[link_parser] LLM 函数 parse_video 注册成功", level="info")
    except Exception as e:
        ctx.log(f"[link_parser] LLM 函数注册失败(llm_core 未加载?): {e}", level="warning")


def _fn_parse_video(args):
    """LLM 函数 handler:全同步实现(llm_core 在线程池中调用)"""
    url = str(args.get("url") or "").strip()
    if not url:
        return "错误:缺少 url 参数(视频分享链接)"

    if classify(url) == "bili":
        m = _BILI_CODE_RE.search(url)
        code = m.group(1) if m else None
        if not code and "b23.tv" in url:
            try:
                with httpx.Client(timeout=15, headers=_BILI_HEADERS,
                                  follow_redirects=True) as client:
                    r = client.get(url)
                m = _BILI_CODE_RE.search(str(r.url))
                code = m.group(1) if m else None
            except Exception:
                code = None
        if not code:
            return "错误:未能从链接中识别 B站视频号,请让用户确认链接"
        try:
            with httpx.Client(timeout=20, headers=_BILI_HEADERS,
                              follow_redirects=True) as client:
                r = client.get(_BILI_API, params=_bili_params(code))
                data = r.json()
        except Exception:
            return "错误:B站接口请求失败,请稍后再试"
        if data.get("code") != 0 or not data.get("data"):
            return f"错误:B站视频未找到({data.get('message', '未知错误')})"
        text, pic = _bili_parse_payload(data)
        d = data["data"]
        stat = d.get("stat", {})
        return json.dumps({
            "platform": "B站", "title": d.get("title", ""),
            "author": d.get("owner", {}).get("name", ""),
            "cover": pic, "duration": _fmt_dur(d.get("duration")),
            "view": stat.get("view"), "like": stat.get("like"),
            "danmaku": stat.get("danmaku"), "summary": text[:500],
        }, ensure_ascii=False)

    info, err = video_fetch_sync(url)
    if info is None:
        return ("错误:视频解析未配置(fengyu_api_key),请在插件配置中填写"
                if err == "not_configured" else
                "错误:视频解析失败(链接无效或接口异常),请让用户确认链接")
    plat = PLATFORM_NAMES.get(info["platform"].lower(), info["platform"] or "未知")
    has = info.get("has_count", False)
    return json.dumps({
        "platform": plat, "type": info.get("type", ""),
        "title": info.get("title", ""), "author": info.get("author", ""),
        "cover": info.get("cover", ""),
        "like": info.get("like") if has else None,
        "comment": info.get("comment") if has else None,
        "share": info.get("share") if has else None,
        "collect": info.get("collect") if has else None,
        "interaction_note": "" if has else "该平台接口未返回互动数据(如B站)",
    }, ensure_ascii=False)


# ================= 帮助与注册 =================

async def handle_help(event, match):
    await _areply(event, "\n".join([
        "━━━ 链接解析 ━━━",
        "/bv <BV|AV号>  解析 B 站视频",
        "/解析 <链接>   解析视频分享链接",
        "/分析 <url>    分析网页",
        "分析 开|关      群内自动网页分析开关(管理员)",
        "自动解析:B站/视频平台链接发进群即出卡片",
        "━━━━━━━━━━━━",
    ]))


async def _areply(event, msg):
    if event is None:
        return
    await ctx.asend_msg(
        user_id=getattr(event, "user_id", None),
        group_id=event.group_id if getattr(event, "is_group", False) else None,
        message=msg)


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/bv\\s+([\\s\\S]+)", handle_bv_cmd, priority=45,
                alias="/av\\s+([\\s\\S]+)", description="解析 B 站视频")
    ctx.command("/解析", handle_parse_cmd, priority=45, alias="/视频解析",
                description="解析视频链接: /解析 <分享链接>")
    ctx.command("/分析\\s+([\\s\\S]+)", handle_analyze_cmd, priority=45,
                description="分析网页: /分析 <URL>")
    ctx.command(r"^/?(分析|网页分析)\s*(开|关)\s*$", handle_web_toggle, priority=45,
                require_admin=True, description="群内开关自动网页分析")
    ctx.command("/链接帮助", handle_help, priority=45, alias="/链接解析帮助",
                description="链接解析使用帮助")
    ctx.on_raw_message(on_raw)
    _try_register_llm()
    ctx.on("system.plugin.loaded", lambda *_a: _try_register_llm())
    ctx.log("[link_parser] 链接解析中枢已注册(B站/视频/网页三分支)", level="info")

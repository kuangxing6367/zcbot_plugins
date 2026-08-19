"""
视频链接解析插件 - 自动解析群内分享的视频链接，返回视频信息（封面图+文字），不返回视频直链
支持平台：抖音 / 快手 / B站 / 小红书 / 微博 / 西瓜 / 皮皮虾 / 腾讯 / YouTube 等（枫雨API 聚合解析）
同时注册为 LLM AI 函数：AI 可调用 parse_video 解析链接，基于返回信息继续回答（AI 可把封面图发给用户）

API: https://api-v2.yuafeng.cn/API/juhejx.php?url=<分享链接>&apikey=<key>
返回 JSON: {code:0, msg:"获取成功", data:{author, music, count, desc, cover, url, video}, type, platform}
"""
import json
import os
import re
import tempfile
import threading
import time

import requests

__plugin_meta__ = {
    "name": "视频解析",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "自动解析群内视频分享链接，返回信息卡片（封面图+文字，不含直链）；已注册为 LLM AI 函数",
    "priority": 80,
}

# ---------- 默认配置（可在 WebUI 插件面板修改） ----------
API_URL = "https://api-v2.yuafeng.cn/API/juhejx.php"
API_KEY = "2afaba9725545fc7736adb1cba1fe873597f15c2cfa9ffc41d700936a9f1e4ac"
TIMEOUT = 10
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 视频平台域名（命中才自动解析，避免拦截普通链接）
VIDEO_HOSTS = re.compile(
    r"https?://[^\s]+?(?:"
    r"douyin\.com|iesdouyin\.com|kuaishou\.com|bilibili\.com|b23\.tv|"
    r"xiaohongshu\.com|xhslink\.com|weibo\.com|ixigua\.com|pipix\.com|"
    r"pearvideo\.com|v\.qq\.com|youtube\.com|youtu\.be"
    r")[^\s]*",
    re.IGNORECASE,
)

# 平台代号 → 中文名（枫雨API 返回 platform 字段，可能是代号也可能是中文）
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

ctx = None
_llm_registered = False
_cooldown_map = {}          # (group_id, url) -> 最近解析时间戳
# 冷却记录上限：超出后清理过期条目，防长期运行内存无限增长
_COOLDOWN_MAP_MAX = 5000
_cooldown_checks = 0
_cooldown_lock = threading.Lock()


def register(ctx_arg):
    """插件注册入口"""
    global ctx
    ctx = ctx_arg
    # 自动解析：群消息里出现视频平台链接即触发（正则 search 匹配，任意位置）
    ctx.command(
        VIDEO_HOSTS.pattern, handle_auto,
        priority=80,
        description="自动解析视频分享链接（抖音/快手/B站/小红书/微博等）",
    )
    # 手动命令：/解析 <链接>
    ctx.command(
        "/解析", handle_manual,
        priority=80,
        alias=["/视频解析"],
        description="解析视频链接，用法: /解析 <分享链接>",
    )
    # 注册 LLM AI 函数（llm_chat 可能尚未加载，注册失败则等插件加载完成事件重试）
    _try_register_llm()
    ctx.on("system.plugin.loaded", _on_plugins_loaded)
    ctx.log("[video_parse] 视频解析插件已注册（自动解析 + /解析 命令）", level="info")


def on_error(event, error):
    """框架生命周期钩子：handler 异常时记录详细日志"""
    try:
        ctx.log(f"[video_parse] handler 异常: {error}", level="error")
    except Exception:
        pass


# ================= 配置读取 =================

def _get_config(key, default=None):
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


# ================= 链接检测 =================

def handle_auto(event, match):
    """自动解析：群/私聊消息里出现视频平台链接时触发"""
    if not _get_config("auto_parse", True):
        return False
    if event.is_group:
        if not _get_config("group_auto_parse", True):
            return False
    else:
        if not _get_config("private_auto_parse", True):
            return False
    text = event.message or ""
    # 被 @ 的消息让给 LLM 对话处理，不抢话
    if text.lstrip().startswith("[@"):
        return False
    urls = _extract_urls(event)
    if not urls:
        return False
    ctx.log(f"[video_parse] 消息命中视频链接: urls={[u[:60] for u in urls]} "
            f"group={event.group_id if event.is_group else '-'} user={event.user_id}", level="debug")
    for u in urls:
        _handle_parse(event, u, auto=True)
    return None  # 已处理


def _extract_urls(event):
    """从事件中提取所有可能的视频链接：
    1) event.message 纯文本里的链接
    2) 原始消息段（segments）里 text / share / json 卡片中的链接
    （share/json 卡片的 url 可能被框架文本提取丢弃，这里兜底补全）
    """
    urls = []
    text = event.message or ""
    m = VIDEO_HOSTS.search(text)
    if m:
        urls.append(m.group(0))
    for seg in getattr(event, "segments", None) or []:
        try:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type", "")
            data = seg.get("data", {}) or {}
            if seg_type == "share" and data.get("url"):
                u = str(data["url"])
                if VIDEO_HOSTS.search(u):
                    urls.append(u)
            elif seg_type == "text" and data.get("text"):
                mm = VIDEO_HOSTS.search(str(data["text"]))
                if mm:
                    urls.append(mm.group(0))
            elif seg_type == "json":
                for u in re.findall(r'https?://[^\s"\'<>\\]+', str(data.get("data", ""))):
                    if VIDEO_HOSTS.search(u):
                        urls.append(u)
        except Exception:
            continue
    # 去重保序 + 去掉尾随标点
    seen, out = set(), []
    for u in urls:
        u = (u or "").strip().rstrip(".,;!?，。；！？)")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def handle_manual(event, match):
    """手动命令：/解析 <链接>（群里/私聊均可）"""
    urls = _extract_urls(event)
    if not urls:
        _reply(event, "用法：/解析 <视频分享链接>\n支持抖音/快手/B站/小红书/微博/西瓜/皮皮虾等")
        return None
    _handle_parse(event, urls[0], auto=False)
    return None


# ================= 解析核心 =================

def _handle_parse(event, url, auto=False):
    """统一解析入口：auto=True 走冷却防刷屏，manual 不限制"""
    url = (url or "").strip().rstrip(".,;!?，。；！？)")
    if not url:
        return
    if auto:
        gid = event.group_id if event.is_group else None
        key = (gid, url)
        cd = int(_get_config("cooldown_seconds", 30) or 0)
        if cd > 0:
            with _cooldown_lock:
                now = time.time()
                last = _cooldown_map.get(key, 0)
                if now - last < cd:
                    return  # 冷却中，不重复解析
                _cooldown_map[key] = now
                # 惰性上限清理：每 128 次检查一次规模，超限剔除冷却已结束的条目
                global _cooldown_checks
                _cooldown_checks += 1
                if _cooldown_checks % 128 == 0 and len(_cooldown_map) > _COOLDOWN_MAP_MAX:
                    expired = [k for k, t in _cooldown_map.items() if now - t >= cd]
                    for k in expired:
                        _cooldown_map.pop(k, None)
    ctx.log(f"[video_parse] 开始解析视频: url={url[:80]} auto={auto} "
            f"group={event.group_id if event.is_group else '-'} user={event.user_id}")
    info = _parse_video(url)
    if info is None:
        if not auto:
            _reply(event, "视频解析失败，请稍后再试~（链接可能无效或接口繁忙）")
        return
    _reply_video_info(event, info)


def _parse_video(url):
    """调用枫雨API 聚合视频解析，返回精简信息 dict；失败返回 None
    兼容不同平台返回结构差异（实测）：
      - 快手/抖音: author、count 在 data 内，标题字段为 desc
      - B站:       author 在【顶层】，无 count 字段，标题字段为 title
    """
    try:
        resp = requests.get(
            _get_config("api_url", API_URL),
            params={"url": url, "apikey": _get_config("api_key", API_KEY)},
            timeout=int(_get_config("timeout", TIMEOUT) or TIMEOUT),
            headers={"User-Agent": UA},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        ctx.log(f"[video_parse] API 请求失败: {e}", level="error")
        return None
    if data.get("code") != 0:
        ctx.log(f"[video_parse] API 返回错误: code={data.get('code')} msg={data.get('msg')}",
                level="warning")
        return None
    d = data.get("data") or {}
    # author：优先 data 内，回退顶层（B站作者在顶层）
    author = d.get("author") or data.get("author") or {}
    # count：优先 data 内，回退顶层；不存在则为空 dict（该平台无互动数据，如B站）
    raw_count = d.get("count")
    if not isinstance(raw_count, dict):
        raw_count = data.get("count")
    if not isinstance(raw_count, dict):
        raw_count = {}
    # 标题：优先 title，回退 desc（B站有 title；快手等只有 desc）
    title = (d.get("title") or d.get("desc") or "").strip()
    info = {
        "title": title,
        "author": (author.get("name") or "").strip(),
        "platform": str(data.get("platform", "") or ""),
        "type": str(data.get("type", "") or ""),
        "cover": (d.get("cover") or "").strip(),
        "like": _pick_count(raw_count, "like", "likes", "digg", "zan"),
        "comment": _pick_count(raw_count, "comment", "comments"),
        "share": _pick_count(raw_count, "share", "shares"),
        "collect": _pick_count(raw_count, "collect", "favorite", "favorites"),
        "has_count": any(_pick_count(raw_count, k) for k in
                         ("like", "likes", "digg", "zan", "comment",
                          "comments", "share", "shares",
                          "collect", "favorite", "favorites")),
    }
    ctx.log(f"[video_parse] 解析成功: platform={info['platform']} title={info['title'][:40]} "
            f"author={info['author']} cover={'有' if info['cover'] else '无'} "
            f"count={'有' if info['has_count'] else '无'}")
    return info


def _pick_count(d, *keys):
    """从多个候选键中取第一个非空值（兼容不同平台 count 键名），取不到返回 0"""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            try:
                return int(float(str(v).replace("万", "0000").replace("亿", "00000000")))
            except (TypeError, ValueError):
                return v
    return 0


def _reply_video_info(event, info):
    """发送视频信息卡片：封面图（可选）+ 文字，不包含任何视频直链"""
    plat = PLATFORM_NAMES.get(info["platform"].lower(), info["platform"] or "未知")
    max_len = int(_get_config("max_title_len", 100) or 100)
    lines = ["📹 视频解析"]
    head = f"平台：{plat}"
    if info.get("type"):
        head += f"｜类型：{info['type']}"
    lines.append(head)
    if info.get("title"):
        title = info["title"] if len(info["title"]) <= max_len else info["title"][:max_len] + "…"
        lines.append(f"标题：{title}")
    if info.get("author"):
        lines.append(f"作者：{info['author']}")
    if info.get("has_count"):
        lines.append(f"👍{_fmt(info['like'])} 💬{_fmt(info['comment'])} "
                     f"🔄{_fmt(info['share'])} ⭐{_fmt(info['collect'])}")
    text = "\n".join(lines)

    cover = info.get("cover", "")
    if cover and _get_config("send_cover", True):
        img_path = _download_image(cover)
        if img_path:
            _reply(event, f"[CQ:image,file=file:///{img_path}]\n{text}")
            _schedule_cleanup(img_path, delay=15)
            return
    _reply(event, text)


def _download_image(url):
    """下载封面图到临时文件，返回路径；失败返回 None"""
    try:
        resp = requests.get(url, timeout=8, headers={"User-Agent": UA})
        resp.raise_for_status()
        if len(resp.content) < 100:
            return None
        fd, path = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            f.write(resp.content)
        return path
    except Exception as e:
        ctx.log(f"[video_parse] 封面下载失败: {e}", level="warning")
        return None


def _schedule_cleanup(path, delay=15):
    """延迟删除临时文件（等 OneBot 客户端读完后）"""
    def _rm():
        try:
            os.unlink(path)
        except Exception:
            pass
    threading.Timer(delay, _rm).start()


# ================= LLM AI 函数 =================

def _on_plugins_loaded(payload):
    """监听插件加载完成事件，补注册 LLM 函数（llm_chat 加载晚于本插件时）"""
    _try_register_llm()


def _try_register_llm():
    global _llm_registered
    if _llm_registered:
        return
    try:
        import plugin_llm_chat as llm_mod  # 跨插件：模块名 plugin_<插件名>
        llm_mod.register_llm_function(
            name="parse_video",
            description=(
                "解析视频分享链接（抖音/快手/B站/小红书/微博/西瓜/皮皮虾等），"
                "返回视频标题、作者、平台、互动数据与封面图地址。"
                "用户发来视频链接或询问视频内容时调用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "视频分享链接"},
                },
                "required": ["url"],
            },
            handler=_fn_parse_video,
            plugin_name="video_parse",
        )
        _llm_registered = True
        ctx.log("[video_parse] LLM 函数 parse_video 注册成功", level="info")
    except Exception as e:
        ctx.log(f"[video_parse] LLM 函数注册失败（llm_chat 未加载?）: {e}", level="warning")


def _fn_parse_video(args, _ctx, _event, _user_id):
    """LLM 函数 handler：解析链接，返回信息 JSON（含封面图URL，供 AI 发图/描述；不含视频直链）"""
    try:
        url = str(args.get("url") or "").strip()
    except Exception:
        url = ""
    if not url:
        return "错误：缺少 url 参数（视频分享链接）"
    info = _parse_video(url)
    if info is None:
        return "错误：视频解析失败（链接无效或接口异常），请让用户确认链接"
    plat = PLATFORM_NAMES.get(info["platform"].lower(), info["platform"] or "未知")
    has = info.get("has_count", False)
    return json.dumps({
        "platform": plat,
        "type": info.get("type", ""),
        "title": info.get("title", ""),
        "author": info.get("author", ""),
        "cover": info.get("cover", ""),
        "like": info.get("like") if has else None,
        "comment": info.get("comment") if has else None,
        "share": info.get("share") if has else None,
        "collect": info.get("collect") if has else None,
        "interaction_note": "" if has else "该平台接口未返回互动数据（如B站）",
    }, ensure_ascii=False)


# ================= 工具 =================

def _fmt(n):
    """数字友好格式化：13.9万 / 1.2亿"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def _reply(event, message):
    """按消息来源（群/私聊）回复"""
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=message,
    )

"""
LLM 文件发送插件
====================================
给对话模型一只「手」：给它一个 URL，它把文件下载下来直接发到当前会话。
按扩展名自动选择发送形态——图片走图片、音频走语音、视频走视频，其余当文件发。

用法：
  1. 命令：/发文件 <url> [文件名]
  2. LLM 函数：模型在对话中自行调用 send_file_from_url(url, filename)

数据：
  - 下载落盘到插件数据目录的 uploads/ 子目录（ctx.get_data_dir()）
  - 落盘文件按 retain_hours 定时清理，避免无限堆积

配置项见 _conf_schema.json。
"""
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

__plugin_meta__ = {
    "name": "LLM文件发送",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "给 LLM 一个发送文件的函数：按 URL 下载并发送图片/语音/视频/文件，同时提供 /发文件 命令",
    "priority": 55,
}

_ctx = None
_llm_registered = False

_IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.ico'}
_AUDIO_EXT = {'.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.amr', '.silk'}
_VIDEO_EXT = {'.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv'}

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/122.0 Safari/537.36')


def register(ctx):
    """插件注册入口"""
    global _ctx
    _ctx = ctx

    try:
        os.makedirs(_upload_dir(), exist_ok=True)
    except Exception as e:  # noqa: BLE001
        ctx.log(f"llm_file_upload 创建上传目录失败: {e}", level="warning")

    # llm_core 可能晚于本插件加载，注册失败就等下次插件加载事件再试
    if not _try_register_llm():
        try:
            ctx.on("system.plugin.loaded", _try_register_llm)
        except Exception:  # noqa: BLE001
            pass

    ctx.command(r"^/发文件\s+(\S+)(?:\s+(\S+))?\s*$", handle_send_command, priority=55,
                description="按 URL 下载并发送文件，用法: /发文件 <url> [文件名]")
    ctx.task("17 * * * *", cleanup_expired, description="清理过期的上传临时文件")
    ctx.log("llm_file_upload 已注册：/发文件 + LLM 函数 send_file_from_url")


# ===================== LLM 函数 =====================

def _try_register_llm(*_args):
    """把发送文件的函数挂到 llm_core 工具总线"""
    global _llm_registered
    if _llm_registered or _ctx is None:
        return _llm_registered
    if not _cfg_bool("enable_llm", True):
        return False
    try:
        svc = _ctx._framework.services.get('llm_core')
        if svc is None:
            raise RuntimeError("llm_core 服务未加载")

        @svc.tool(
            name="send_file_from_url",
            description=("根据给定 URL 下载文件并直接发送到当前会话。"
                         "当用户要求发送/转发某个链接里的图片、语音、视频或文件时调用；"
                         "自己拿不到文件内容、只能拿到网址时也用它。"
                         "参数 url 为文件地址；filename 可选，用于指定发送时显示的文件名。"))
        def send_file_from_url(url: str, filename: str = '', event=None):
            return send_url(url, filename, event)
        _llm_registered = True
        _ctx.log("[llm_file_upload] LLM 函数 send_file_from_url 注册成功")
        return True
    except Exception as e:  # noqa: BLE001
        _ctx.log(f"[llm_file_upload] LLM 函数注册失败（llm_core 未加载?）: {e}", level="warning")
        return False


# ===================== 命令 =====================

async def handle_send_command(event, match):
    """命令入口：/发文件 <url> [文件名]"""
    url = (match.group(1) or '').strip() if match else ''
    filename = (match.group(2) or '').strip() if match else ''
    if not url:
        await _reply(event, "用法：/发文件 <url> [文件名]")
        return
    result = await _ctx.run_async(send_url, url, filename, event)
    if not result.startswith("已发送"):
        await _reply(event, result)


async def cleanup_expired():
    """清理超过保留时长的临时文件"""
    hours = _cfg_int("retain_hours", 24)
    if hours <= 0:
        return
    deadline = time.time() - hours * 3600
    try:
        removed = 0
        for item in Path(_upload_dir()).glob('*'):
            try:
                if item.is_file() and item.stat().st_mtime < deadline:
                    item.unlink()
                    removed += 1
            except OSError:
                continue
        if removed:
            _ctx.log(f"llm_file_upload 清理过期文件 {removed} 个")
    except Exception as e:  # noqa: BLE001
        _ctx.log(f"llm_file_upload 清理失败: {e}", level="warning")


# ===================== 核心逻辑 =====================

def send_url(url: str, filename: str = '', event=None) -> str:
    """下载 URL 指向的文件并发送到指定会话，返回给模型/用户的结果文本"""
    if _ctx is None:
        return "错误：插件尚未初始化"
    url = (url or '').strip()
    if not url:
        return "错误：缺少 url 参数"
    if not url.startswith(('http://', 'https://')):
        return "错误：url 必须以 http:// 或 https:// 开头"

    filename = (filename or '').strip() or _guess_filename(url)
    if not _ext_allowed(filename):
        return f"错误：文件类型 {Path(filename).suffix or '(无扩展名)'} 不在允许列表内"

    max_mb = _cfg_int("max_file_size", 50)
    timeout = _cfg_int("timeout", 30)
    try:
        data, final_name = _download(url, filename, max_mb, timeout)
    except Exception as e:  # noqa: BLE001
        return f"下载失败：{e}"

    path = _save(data, final_name)
    kind = _segment_kind(final_name)
    size_mb = len(data) / 1024 / 1024
    cq = f"[CQ:{kind},file=file:///{path.as_posix()}]"

    target = _target_of(event)
    if target is None:
        return "错误：无法判断发送目标（既不在群里，也拿不到用户 ID）"
    try:
        _ctx.send_msg(user_id=target[0], group_id=target[1], message=cq)
    except Exception as e:  # noqa: BLE001
        return f"发送失败：{e}"
    return f"已发送{kind_label(kind)}：{final_name}（{size_mb:.2f} MB）"


def _download(url: str, filename: str, max_mb: int, timeout: int):
    """下载并做体积校验，返回 (bytes, 文件名)"""
    req = urllib.request.Request(url, headers={'User-Agent': _UA})
    limit = max_mb * 1024 * 1024
    chunks = []
    total = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        declared = resp.headers.get('Content-Length')
        if declared and declared.isdigit() and int(declared) > limit:
            raise ValueError(f"文件超过 {max_mb} MB 上限（{int(declared) / 1024 / 1024:.1f} MB）")
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError(f"文件超过 {max_mb} MB 上限")
            chunks.append(chunk)
        if not filename or filename == 'download':
            disposition = resp.headers.get('Content-Disposition') or ''
            filename = _name_from_disposition(disposition) or filename
    return b''.join(chunks), filename or 'download'


def _save(data: bytes, filename: str) -> Path:
    """落盘（同名自动加序号，避免覆盖正在发送中的文件）"""
    directory = Path(_upload_dir())
    directory.mkdir(parents=True, exist_ok=True)
    stem = Path(filename).name or 'download'
    target = directory / stem
    if target.exists():
        target = directory / f"{int(time.time())}_{stem}"
    with open(target, 'wb') as f:
        f.write(data)
    return target


# ===================== 工具函数 =====================

def kind_label(kind: str) -> str:
    return {'image': '图片', 'record': '语音', 'video': '视频'}.get(kind, '文件')


def _segment_kind(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in _IMAGE_EXT:
        return 'image'
    if ext in _AUDIO_EXT:
        return 'record'
    if ext in _VIDEO_EXT:
        return 'video'
    return 'file'


def _guess_filename(url: str) -> str:
    name = Path(urllib.parse.urlsplit(url).path).name
    return urllib.parse.unquote(name) or 'download'


def _name_from_disposition(disposition: str) -> str:
    marker = 'filename='
    pos = disposition.find(marker)
    if pos < 0:
        return ''
    raw = disposition[pos + len(marker):].strip().strip('"').split(';')[0]
    return urllib.parse.unquote(raw) or ''


def _ext_allowed(filename: str) -> bool:
    allowed = _cfg_str("allowed_extensions", "")
    if not allowed.strip():
        return True
    ext = Path(filename).suffix.lower().lstrip('.')
    return ext in {e.strip().lower().lstrip('.') for e in allowed.split(',') if e.strip()}


def _target_of(event):
    """从事件解析发送目标 (user_id, group_id)；拿不到则返回 None"""
    if event is None:
        return None
    group_id = getattr(event, 'group_id', None) if getattr(event, 'is_group', False) else None
    user_id = getattr(event, 'user_id', None)
    if not user_id and not group_id:
        return None
    return user_id, group_id


def _upload_dir() -> str:
    base = _ctx.get_data_dir() if _ctx else '.'
    return os.path.join(base, 'uploads')


async def _reply(event, message):
    try:
        _ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if getattr(event, 'is_group', False) else None,
            message=message,
        )
    except Exception as e:  # noqa: BLE001
        _ctx.log(f"llm_file_upload 回复失败: {e}", level="warning")


def _cfg_str(key, default=''):
    try:
        value = _ctx.get_config(key, default)
    except Exception:  # noqa: BLE001
        return default
    return default if value is None else str(value)


def _cfg_int(key, default=0):
    try:
        return int(_cfg_str(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _cfg_bool(key, default=False):
    return _cfg_str(key, 'true' if default else 'false').strip().lower() in ('1', 'true', 'yes', 'on')

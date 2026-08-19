"""
文件操作插件 - 文件发送、删除、移动、复制、查看文件夹内容、删除目录、上传、帮助
从 AstrBot 迁移至 zgric_onebot11 新语法

命令（全部需要超级管理员权限）：
  /发送文件 <路径>              发送指定路径的文件
  /删除文件 <路径>              删除指定路径的文件
  /删除目录 <路径>              删除指定路径的目录（含所有内容）
  /移动文件 <源路径> <目标路径>  移动文件或目录
  /复制文件 <源路径> <目标路径>  复制文件或目录
  /文件列表 [路径]              查看目录内容（默认基础路径）
  /上传 <后缀名> <目标路径>      上传文件，60秒内等待用户发送文件
  /插件路径                     显示插件路径
  /文件帮助                     显示帮助信息

配置项（_conf_schema.json）：
  base_path  字符串  文件操作基础路径，默认 ./data/files

安全要求：
  - 所有命令仅超级管理员可用（ctx.is_superuser）
  - 路径规范化：禁止路径穿越（包含 .. 的路径被拒绝）
  - 文件大小限制：2GB
"""
import os
import shutil
import time
import threading

__plugin_meta__ = {
    "name": "文件操作",
    "version": "1.3.0",
    "author": "Chris",
    "desc": "文件发送、删除、移动、复制、查看文件夹内容、删除目录、上传（仅超管）",
    "priority": 50,
}

# 文件大小上限：2GB
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024

# 图片扩展名集合（用于决定使用 [CQ:image] 还是 [CQ:file]）
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico"}

# 上传等待状态 {uid: {'time': time, 'path': str, 'extension': str}}
_upload_waiting = {}
_upload_lock = threading.Lock()


def register(ctx):
    """插件注册入口"""
    ctx.command("/发送文件", handle_send_file, priority=50, require_superuser=True,
                description="发送指定路径的文件：/发送文件 <路径>")
    ctx.command("/删除文件", handle_delete_file, priority=50, require_superuser=True,
                description="删除指定路径的文件：/删除文件 <路径>")
    ctx.command("/删除目录", handle_delete_directory, priority=50, require_superuser=True,
                description="删除指定路径的目录：/删除目录 <路径>")
    ctx.command("/移动文件", handle_move_file, priority=50, require_superuser=True,
                description="移动文件或目录：/移动文件 <源路径> <目标路径>")
    ctx.command("/复制文件", handle_copy_file, priority=50, require_superuser=True,
                description="复制文件或目录：/复制文件 <源路径> <目标路径>")
    ctx.command("/文件列表", handle_list_files, priority=50, require_superuser=True,
                description="查看目录内容：/文件列表 [路径]")
    ctx.command("/上传", handle_upload, priority=50, require_superuser=True,
                description="上传文件：/上传 <后缀名> <目标路径>（60秒内发送文件）")
    ctx.command("/插件路径", handle_plugin_path, priority=50, require_superuser=True,
                description="显示插件路径")
    ctx.command("/文件帮助", handle_help, priority=50, require_superuser=True,
                description="显示文件插件帮助信息")

    # 订阅消息事件，用于接收文件上传
    try:
        ctx.on("message", on_file_message)
        ctx.logger.info("文件插件已订阅消息事件，用于文件上传功能")
    except Exception as e:
        ctx.logger.warning(f"订阅消息事件失败，上传功能可能不可用: {e}")


# ==================== 工具函数 ====================

def _get_base_path(ctx):
    """读取配置的基础路径，默认 ./data/files"""
    base = ctx.get_config("base_path", "./data/files")
    if not base:
        base = "./data/files"
    # 规范化为绝对路径，便于后续拼接
    return os.path.normpath(os.path.abspath(base))


def _is_path_traversal(input_path: str) -> bool:
    """检测路径穿越：包含 .. 路径段即视为穿越"""
    if not input_path:
        return False
    # 统一分隔符后按段检查
    normalized = input_path.replace("\\", "/")
    parts = normalized.split("/")
    return ".." in parts


def _normalize_path(ctx, input_path: str):
    """
    规范化路径：
    - 禁止 .. 路径穿越（返回 (None, error_msg)）
    - 绝对路径直接使用；相对路径拼接到 base_path
    - 返回 (abs_path, None) 或 (None, error_msg)
    """
    if input_path is None:
        return None, "路径不能为空"

    input_path = input_path.strip()
    if not input_path:
        return None, "路径不能为空"

    if _is_path_traversal(input_path):
        return None, "禁止路径穿越：路径中不能包含 .."

    # 统一分隔符
    normalized = input_path.replace("\\", "/").replace("//", "/")

    if os.path.isabs(input_path):
        abs_path = os.path.normpath(normalized)
    else:
        base = _get_base_path(ctx)
        abs_path = os.path.normpath(os.path.join(base, normalized))

    return abs_path, None


def _file_url(abs_path: str) -> str:
    """将本地绝对路径转换为 file:// URL（OneBot CQ 码用）"""
    # Windows: C:\path\to\file -> file:///C:/path/to/file
    # Linux:   /path/to/file    -> file:///path/to/file
    p = abs_path.replace("\\", "/")
    if p.startswith("/"):
        return f"file://{p}"
    # 形如 C:/... 需要三个斜杠
    return f"file:///{p}"


def _reply(ctx, event, text):
    """快捷回复消息（群聊回群、私聊回私）"""
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=text,
    )


def _require_superuser(ctx, event):
    """权限校验：非超管拒绝并返回 False"""
    if not ctx.is_superuser(event.user_id):
        _reply(ctx, event, " 此命令仅限超级管理员使用。")
        return False
    return True


def _parse_args(match, event, expected_min):
    """
    从 match.group(1) 解析按空白切分的参数。
    expected_min 为期望的最少参数个数。
    返回 (args_list, error_msg)；失败时 args_list 为 None。
    """
    raw = ""
    if match:
        try:
            raw = (match.group(1) or "").strip()
        except (IndexError, AttributeError):
            raw = ""
    if not raw:
        return None, "缺少参数，请参考命令说明。"

    parts = raw.split()
    if len(parts) < expected_min:
        return None, f"参数不足，期望 {expected_min} 个，实际 {len(parts)} 个。"
    return parts, None


def _format_size(size):
    """格式化文件大小"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.2f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.2f} MB"
    return f"{size / 1024 / 1024 / 1024:.2f} GB"


def _get_plugin_path():
    """获取插件路径（main.py 所在目录的上级目录）"""
    try:
        current_file = os.path.abspath(__file__)
        # 获取上上级目录（plugins/file/main.py -> plugins/file -> plugins）
        # 根据源插件逻辑，插件路径为主文件的上级目录的上级目录
        # 但这里更合理的是显示插件所在目录的父目录
        plugin_dir = os.path.dirname(current_file)
        parent_dir = os.path.dirname(plugin_dir)
        return parent_dir
    except Exception as e:
        return f"获取插件路径失败: {e}"


def _file_url_to_local_path(file_url: str) -> str:
    """
    将 file:/// URL 转换为本地绝对路径。
    例如: file:///C:/path/file.txt -> C:\path\file.txt
    """
    if not file_url:
        return ""

    # 移除 file:// 前缀
    path = file_url
    if path.startswith("file:///"):
        path = path[8:]
    elif path.startswith("file://"):
        path = path[7:]

    # 在 Windows 上，/C:/path 这样的路径需要去掉开头的 /
    # 在 Linux 上，/path 就是正确的
    path = path.replace("/", "\\")
    return path


def _get_file_from_event(event):
    """
    从事件中提取文件信息。
    返回 (file_path, file_name) 或 None。
    适用于 OneBot 11 消息段格式。
    """
    message = event.message
    if not message:
        return None

    # 消息可能是列表（消息段数组）或字符串
    if isinstance(message, list):
        for seg in message:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type", "")
            seg_data = seg.get("data", {})
            if seg_type in ("file", "image", "video"):
                file_url = seg_data.get("file", "")
                file_name = seg_data.get("name", "")
                if not file_name and file_url:
                    # 从 URL 中提取文件名
                    file_name = os.path.basename(file_url)
                if file_url:
                    return file_url, file_name
    elif isinstance(message, str):
        # 字符串消息，尝试查找 CQ 码中的 file 信息
        import re as _re
        # 匹配 [CQ:file,file=xxx] 或 [CQ:image,file=xxx]
        cq_match = _re.search(r'\[CQ:(?:file|image|video),file=([^\]]+)\]', message)
        if cq_match:
            file_url = cq_match.group(1)
            file_name = os.path.basename(file_url)
            return file_url, file_name

    return None


def _read_file_content(file_url: str) -> bytes:
    """
    从 OneBot 文件 URL 读取文件内容。
    支持 file:/// 协议和 base64:// 协议。
    """
    if not file_url:
        return None

    # base64 编码的文件
    if file_url.startswith("base64://"):
        import base64
        try:
            b64_data = file_url[len("base64://"):]
            return base64.b64decode(b64_data)
        except Exception:
            return None

    # file:// 协议的文件
    if file_url.startswith("file://"):
        local_path = _file_url_to_local_path(file_url)
        if os.path.exists(local_path):
            try:
                with open(local_path, "rb") as f:
                    return f.read()
            except Exception:
                return None
        return None

    # 尝试直接作为本地路径读取
    if os.path.exists(file_url):
        try:
            with open(file_url, "rb") as f:
                return f.read()
        except Exception:
            return None

    return None


# ==================== 命令处理 ====================

def handle_send_file(event, match):
    """/发送文件 <路径> - 发送指定路径的文件"""
    if not _require_superuser(ctx, event):
        return

    args, err = _parse_args(match, event, 1)
    if err:
        _reply(ctx, event, f" {err}\n用法：/发送文件 <路径>")
        return

    file_path = args[0]
    abs_path, perr = _normalize_path(ctx, file_path)
    if perr:
        _reply(ctx, event, f" {perr}")
        return

    if not os.path.exists(abs_path):
        _reply(ctx, event, f" 文件 {file_path} 不存在，请检查路径。")
        return

    if os.path.isdir(abs_path):
        _reply(ctx, event, f" 指定路径是目录而非文件：{file_path}")
        return

    file_size = os.path.getsize(abs_path)
    if file_size == 0:
        _reply(ctx, event, f" 文件 {file_path} 是空文件，无法发送。")
        return
    if file_size > MAX_FILE_SIZE:
        _reply(ctx, event, f" 文件 {file_path} 大小超过 2GB 限制，无法发送。")
        return

    # 测试可读
    try:
        with open(abs_path, "rb") as f:
            f.read(1)
    except Exception as e:
        _reply(ctx, event, f" 无法读取文件 {file_path}: {e}")
        return

    file_name = os.path.basename(abs_path)
    url = _file_url(abs_path)
    _, ext = os.path.splitext(file_name)
    ext_lower = ext.lower()

    try:
        if ext_lower in _IMAGE_EXTS:
            cq = f"[CQ:image,file={url}]"
        else:
            cq = f"[CQ:file,file={url}]"
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message=cq,
        )
        _reply(ctx, event,
               f" 文件 {file_name} 已发送（大小: {file_size / 1024:.2f} KB）")
    except Exception as e:
        _reply(ctx, event,
               f" 发送文件失败: {e}\n"
               f"文件绝对路径: {abs_path}")


def handle_delete_file(event, match):
    """/删除文件 <路径> - 删除指定路径的文件"""
    if not _require_superuser(ctx, event):
        return

    args, err = _parse_args(match, event, 1)
    if err:
        _reply(ctx, event, f" {err}\n用法：/删除文件 <路径>")
        return

    file_path = args[0]
    abs_path, perr = _normalize_path(ctx, file_path)
    if perr:
        _reply(ctx, event, f" {perr}")
        return

    if not os.path.exists(abs_path):
        _reply(ctx, event, f" 文件 {file_path} 不存在，请检查路径。")
        return

    if os.path.isdir(abs_path):
        _reply(ctx, event, f" 指定路径是目录而非文件：{file_path}\n"
                           f"（删除目录请使用 /删除目录 命令）")
        return

    try:
        os.remove(abs_path)
        _reply(ctx, event, f" 文件 {file_path} 已删除。")
    except Exception as e:
        _reply(ctx, event, f" 删除文件时发生错误: {e}")


def handle_delete_directory(event, match):
    """/删除目录 <路径> - 删除指定路径的目录（含所有内容）"""
    if not _require_superuser(ctx, event):
        return

    args, err = _parse_args(match, event, 1)
    if err:
        _reply(ctx, event, f" {err}\n用法：/删除目录 <路径>")
        return

    dir_path = args[0]
    abs_path, perr = _normalize_path(ctx, dir_path)
    if perr:
        _reply(ctx, event, f" {perr}")
        return

    if not os.path.exists(abs_path):
        _reply(ctx, event, f" 目录 {dir_path} 不存在，请检查路径。")
        return

    if not os.path.isdir(abs_path):
        _reply(ctx, event, f" 指定路径 {dir_path} 不是一个目录。")
        return

    try:
        shutil.rmtree(abs_path)
        _reply(ctx, event, f" 目录 {dir_path} 已成功删除。")
    except Exception as e:
        _reply(ctx, event, f" 删除目录时发生错误: {e}")


def handle_move_file(event, match):
    """/移动文件 <源路径> <目标路径> - 移动文件或目录"""
    if not _require_superuser(ctx, event):
        return

    args, err = _parse_args(match, event, 2)
    if err:
        _reply(ctx, event, f" {err}\n用法：/移动文件 <源路径> <目标路径>")
        return

    src_path, dst_path = args[0], args[1]
    src_abs, perr = _normalize_path(ctx, src_path)
    if perr:
        _reply(ctx, event, f" 源路径: {perr}")
        return
    dst_abs, perr = _normalize_path(ctx, dst_path)
    if perr:
        _reply(ctx, event, f" 目标路径: {perr}")
        return

    if not os.path.exists(src_abs):
        _reply(ctx, event, f" 源路径 {src_path} 不存在。")
        return

    try:
        # 确保目标父目录存在
        dst_parent = os.path.dirname(dst_abs)
        if dst_parent and not os.path.exists(dst_parent):
            os.makedirs(dst_parent, exist_ok=True)
        shutil.move(src_abs, dst_abs)
        _reply(ctx, event,
               f" 已移动：{src_path} → {dst_path}")
    except Exception as e:
        _reply(ctx, event, f" 移动文件/目录时发生错误: {e}")


def handle_copy_file(event, match):
    """/复制文件 <源路径> <目标路径> - 复制文件或目录"""
    if not _require_superuser(ctx, event):
        return

    args, err = _parse_args(match, event, 2)
    if err:
        _reply(ctx, event, f" {err}\n用法：/复制文件 <源路径> <目标路径>")
        return

    src_path, dst_path = args[0], args[1]
    src_abs, perr = _normalize_path(ctx, src_path)
    if perr:
        _reply(ctx, event, f" 源路径: {perr}")
        return
    dst_abs, perr = _normalize_path(ctx, dst_path)
    if perr:
        _reply(ctx, event, f" 目标路径: {perr}")
        return

    if not os.path.exists(src_abs):
        _reply(ctx, event, f" 源路径 {src_path} 不存在。")
        return

    # 大文件复制前预检
    try:
        if os.path.isfile(src_abs):
            size = os.path.getsize(src_abs)
            if size > MAX_FILE_SIZE:
                _reply(ctx, event,
                       f" 源文件大小超过 2GB 限制（{size / 1024 / 1024 / 1024:.2f} GB）。")
                return
    except Exception:
        pass

    try:
        dst_parent = os.path.dirname(dst_abs)
        if dst_parent and not os.path.exists(dst_parent):
            os.makedirs(dst_parent, exist_ok=True)
        if os.path.isdir(src_abs):
            shutil.copytree(src_abs, dst_abs)
        else:
            shutil.copy2(src_abs, dst_abs)
        _reply(ctx, event,
               f" 已复制：{src_path} → {dst_path}")
    except Exception as e:
        _reply(ctx, event, f" 复制文件/目录时发生错误: {e}")


def handle_list_files(event, match):
    """/文件列表 [路径] - 查看目录内容"""
    if not _require_superuser(ctx, event):
        return

    # 路径可选，默认 base_path
    raw = ""
    if match:
        try:
            raw = (match.group(1) or "").strip()
        except (IndexError, AttributeError):
            raw = ""

    if raw:
        dir_path = raw
        abs_path, perr = _normalize_path(ctx, dir_path)
        if perr:
            _reply(ctx, event, f" {perr}")
            return
    else:
        dir_path = "(基础路径)"
        abs_path = _get_base_path(ctx)

    if not os.path.exists(abs_path):
        _reply(ctx, event, f" 目录 {dir_path} 不存在。\n当前基础路径: {_get_base_path(ctx)}")
        return

    if not os.path.isdir(abs_path):
        _reply(ctx, event, f" 指定路径不是目录：{dir_path}")
        return

    try:
        entries = os.listdir(abs_path)
    except Exception as e:
        _reply(ctx, event, f" 读取目录时发生错误: {e}")
        return

    if not entries:
        _reply(ctx, event, f" 目录 {dir_path} 是空的。\n绝对路径: {abs_path}")
        return

    lines = [f" 目录 {dir_path} 的内容", "━" * 15]
    dir_count = 0
    file_count = 0
    for name in entries:
        full = os.path.join(abs_path, name)
        if os.path.isdir(full):
            lines.append(f" /{name}")
            dir_count += 1
        else:
            try:
                size = os.path.getsize(full)
                lines.append(f" {name}  ({_format_size(size)})")
            except Exception:
                lines.append(f" {name}")
            file_count += 1
    lines.append("━" * 15)
    lines.append(f"共 {dir_count} 个目录，{file_count} 个文件")
    lines.append(f"绝对路径: {abs_path}")

    _reply(ctx, event, "\n".join(lines))


def handle_upload(event, match):
    """/上传 <后缀名> <目标路径> - 上传文件，60秒内等待用户发送文件"""
    if not _require_superuser(ctx, event):
        return

    # 解析参数：/上传 <后缀名> <目标路径>
    args, err = _parse_args(match, event, 2)
    if err:
        _reply(ctx, event, f" {err}\n用法：/上传 <后缀名> <目标路径>\n"
                           f"示例：/上传 .mp4 /Chris")
        return

    extension = args[0]
    target_path = args[1]

    # 处理特殊情况
    if extension == "无后缀":
        extension = ""
    elif not extension.startswith(".") and extension != "":
        extension = "." + extension  # 自动添加点号

    uid = event.user_id

    # 存储等待状态
    with _upload_lock:
        _upload_waiting[uid] = {
            'time': time.time(),
            'path': target_path,
            'extension': extension,
        }

    ext_text = f"后缀名 {extension}" if extension else "无后缀"
    _reply(ctx, event,
           f" 文件上传器: 请在 60s 内上传一个文件，将保存到 {target_path}，文件 {ext_text}。")

    # 启动后台线程，60秒后超时清理
    timeout_thread = threading.Thread(
        target=_upload_timeout_worker,
        args=(uid, event),
        daemon=True,
    )
    timeout_thread.start()


def handle_plugin_path(event, match):
    """/插件路径 - 显示插件路径"""
    if not _require_superuser(ctx, event):
        return

    path = _get_plugin_path()
    _reply(ctx, event, f" 插件基础路径: {path}")


def handle_help(event, match):
    """/文件帮助 - 显示帮助信息"""
    if not _require_superuser(ctx, event):
        return

    help_text = """ 文件操作插件 - 指令说明

━━━━━━━━━━━━━━━━━━
 发送文件
  /发送文件 <路径> - 发送指定路径的文件

 删除文件
  /删除文件 <路径> - 删除指定路径的文件

 删除目录
  /删除目录 <路径> - 删除指定路径的目录（含所有内容）

 文件列表
  /文件列表 [路径] - 查看目录内容（默认基础路径）

 移动文件
  /移动文件 <源路径> <目标路径> - 移动文件或目录

 复制文件
  /复制文件 <源路径> <目标路径> - 复制文件或目录

 上传文件
  /上传 <后缀名> <目标路径> - 上传文件到指定目录
  示例: /上传 .mp4 /Chris  (然后发送文件)
       /上传 无后缀 /data  (无扩展名文件)

 插件路径
  /插件路径 - 显示插件基础路径

 文件帮助
  /文件帮助 - 显示本帮助信息

━━━━━━━━━━━━━━━━━━
安全说明:
• 所有命令仅超级管理员可用
• 禁止路径穿越（路径中不能包含 ..）
• 文件大小限制为 2GB
• 上传文件限制为 50MB"""

    _reply(ctx, event, help_text)


# ==================== 文件上传消息处理 ====================

def on_file_message(event, match=None):
    """
    被动消息处理：检测用户发送的文件，处理文件上传等待队列。
    match 对于事件订阅为 None。
    """
    uid = event.user_id

    # 检查用户是否在等待上传文件
    with _upload_lock:
        if uid not in _upload_waiting:
            return
        waiting_info = _upload_waiting[uid]

    # 尝试从事件中提取文件信息
    file_info = _get_file_from_event(event)
    if not file_info:
        return

    file_url, original_file_name = file_info

    # 读取文件内容
    file_content = _read_file_content(file_url)
    if file_content is None:
        # 尝试使用 OneBot API 下载文件
        try:
            # 尝试通过 get_file API 获取文件
            file_result = ctx.api("get_file", file=file_url)
            if file_result and file_result.get("status") == "ok":
                data = file_result.get("data", {}) or {}
                file_path = data.get("file", "") or data.get("path", "")
                if file_path and os.path.exists(file_path):
                    with open(file_path, "rb") as f:
                        file_content = f.read()
        except Exception:
            pass

    if file_content is None:
        _reply(ctx, event, " 无法读取文件内容，上传失败。")
        return

    # 检查文件大小
    file_size = len(file_content)
    if file_size > 50 * 1024 * 1024:  # 50MB
        _reply(ctx, event, f" 文件大小超过 50MB 限制，无法上传。")
        with _upload_lock:
            _upload_waiting.pop(uid, None)
        return

    if file_size == 0:
        _reply(ctx, event, " 文件是空文件，无法上传。")
        with _upload_lock:
            _upload_waiting.pop(uid, None)
        return

    # 生成文件名: 时间戳 + 扩展名
    target_path = waiting_info['path']
    extension = waiting_info['extension']
    file_name = f"file_{int(time.time())}{extension}"

    # 规范化目标路径
    target_dir_abs, perr = _normalize_path(ctx, target_path)
    if perr:
        _reply(ctx, event, f" 目标路径错误: {perr}")
        with _upload_lock:
            _upload_waiting.pop(uid, None)
        return

    # 确保目标目录存在
    if not os.path.exists(target_dir_abs):
        try:
            os.makedirs(target_dir_abs, exist_ok=True)
        except Exception as e:
            _reply(ctx, event, f" 创建目录失败: {e}")
            with _upload_lock:
                _upload_waiting.pop(uid, None)
            return

    # 写入文件
    full_file_path = os.path.join(target_dir_abs, file_name)
    try:
        with open(full_file_path, "wb") as f:
            f.write(file_content)
    except Exception as e:
        _reply(ctx, event, f" 写入文件失败: {e}")
        with _upload_lock:
            _upload_waiting.pop(uid, None)
        return

    # 完成上传，移除等待状态
    with _upload_lock:
        _upload_waiting.pop(uid, None)

    _reply(ctx, event,
           f" 文件 {file_name} 上传成功到 {target_path} (大小: {file_size / 1024:.2f} KB)")


def _upload_timeout_worker(uid, event):
    """
    上传超时工作线程。
    等待60秒后检查用户是否仍在等待队列中，若是则发送超时提示。
    """
    time.sleep(60)

    with _upload_lock:
        if uid in _upload_waiting:
            _upload_waiting.pop(uid, None)
            try:
                ctx.send_msg(
                    user_id=event.user_id,
                    group_id=event.group_id if event.is_group else None,
                    message=f"⏰ 文件上传器: 未在规定时间内上传文件，上传已取消。",
                )
            except Exception:
                pass
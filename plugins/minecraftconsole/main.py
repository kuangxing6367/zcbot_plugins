"""
Minecraft 控制台插件（zgric_onebot11 新插件语法）
==================================================
通过桥接服务执行 Minecraft 指令，支持白名单添加。

功能：
- /mc-command <命令> [--t=5s]   执行 MC 命令（管理员）
- /添加游戏白名单 <游戏名称>    添加服务器白名单（需满足QQ等级/入群天数）

设计说明：
- RCON 客户端改为同步 socket（源插件用 asyncio）
- 数据库操作改用独立 DatabaseManager（直连 dbcj 库，与 AstrBot 版本共用）
- 白名单表 whitelist_records / whitelist_attempts / ban_records 建在独立数据库中
"""
import os
import sys
import re
import time
import base64
import socket
import datetime

# 将插件目录加入 sys.path，确保模块可被直接 import
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from rcon_client import RconClient, RconConfig, RconAuthError, RconError
from utils import parse_command_args, parse_exec_options, truncate_text
from message_formatter import MessageFormatter
from database import DatabaseManager

__plugin_meta__ = {
    "name": "MC控制台",
    "version": "1.6.1",
    "author": "zgric",
    "desc": "通过桥接服务执行 Minecraft 指令，支持白名单添加",
    "priority": 30,
}

# 模块级实例
_client = None
_client_cfg = None
_db = None


def register(ctx):
    """插件注册入口"""
    global _db

    # 初始化独立数据库连接
    _ensure_db()

    ctx.command("mc-command", handle_mc_command, priority=30,
                description="执行MC命令：/mc-command <命令> [--t=5s]")
    ctx.command("添加游戏白名单", handle_add_whitelist, priority=30,
                description="添加游戏白名单：/添加游戏白名单 <游戏名称>")
    ctx.command("#强制登陆", handle_force_login, priority=30,
                alias="#强制登录,强制登录,强制登陆",
                description="#强制登陆：按QQ匹配玩家并发送 authme forcelogin <玩家>")
    ctx.command("#重置密码", handle_reset_password, priority=30,
                alias="重置密码,#重设密码,重设密码",
                description="#重置密码 <新密码>：重置本人绑定玩家密码（群聊需机器人为管理员；管理员/群主消息不处理）")
    ctx.command("#强制注册", handle_force_register, priority=30,
                alias="强制注册",
                description="#强制注册 <游戏ID> <密码>：仅管理员可用，执行 AuthMe 注册并写入白名单库（无撤回机制）")

    ctx.log("MC控制台插件已加载（独立数据库模式）")


# ====================================================================
#  配置读取
# ====================================================================

def _cfg(key, default=None):
    return ctx.get_config(key, default)


def _is_enabled():
    return bool(_cfg("enabled", True))


def _is_admin(user_id):
    admins = _cfg("admins", [])
    if isinstance(admins, list):
        admin_set = {str(x).strip() for x in admins}
    elif isinstance(admins, str):
        admin_set = {s.strip() for s in admins.split(",") if s.strip()}
    else:
        admin_set = set()
    return str(user_id) in admin_set


def _is_rcon_ready():
    host = _cfg("rcon_host", "")
    port = _cfg("rcon_port", 25580)
    password = _cfg("rcon_password", "")
    return bool(host and port and password.strip())


def _ensure_client():
    """确保 RCON 客户端就绪"""
    global _client, _client_cfg
    if not _is_rcon_ready():
        _client = None
        _client_cfg = None
        return

    cfg = RconConfig(
        host=_cfg("rcon_host", "127.0.0.1"),
        port=int(_cfg("rcon_port", 25580)),
        password=_cfg("rcon_password", ""),
        timeout=float(_cfg("timeout", 5.0)),
        test_on_first_use=bool(_cfg("test_on_first_use", True)),
    )
    if _client_cfg != cfg:
        _client_cfg = cfg
        _client = RconClient(cfg)


def _exec_with_retry(command, wait_ms):
    """带重试的命令执行"""
    global _client, _client_cfg
    if _client is None or _client_cfg is None:
        raise RconError("client not ready")

    max_attempts = int(_cfg("max_attempts", 2))
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _client.exec(command, wait_ms)
        except RconAuthError:
            _client = RconClient(_client_cfg)
            raise
        except Exception as e:
            last_error = e
            ctx.log(f"第 {attempt}/{max_attempts} 次执行失败: {e}", level="warning")
            _client = RconClient(_client_cfg)
            if attempt >= max_attempts:
                break
    raise RconError(str(last_error) if last_error else "unknown error")


# ====================================================================
#  数据库
# ====================================================================

def _ensure_db():
    """确保 DatabaseManager 就绪"""
    global _db
    host = _cfg("db_host", "127.0.0.1")
    port = int(_cfg("db_port", 3306))
    user = _cfg("db_user", "dbcj")
    password = _cfg("db_password", "")
    db_name = _cfg("db_name", "dbcj")
    if not password:
        ctx.log("白名单数据库密码未配置，请在插件配置中填写 db_password", level="warning")
        return
    if _db is None:
        _db = DatabaseManager(host, port, user, password, db_name)
        _db.ensure_tables()


# ====================================================================
#  命令处理
# ====================================================================

def handle_mc_command(event, match):
    """/mc-command <命令> [--t=5s]"""
    fmt = MessageFormatter()

    if not _is_enabled():
        _reply(event, fmt.format_not_enabled())
        return

    if not _is_admin(event.user_id):
        _reply(event, fmt.format_no_permission())
        return

    # 从 match 中提取参数
    raw_args = (match.group(1) or "").strip() if match else ""
    if not raw_args:
        # 兜底：从消息文本解析
        raw_args = parse_command_args(event.message, "mc-command") or ""
    if not raw_args:
        _reply(event, fmt.format_usage())
        return

    options = parse_exec_options(raw_args)
    if not options.command:
        _reply(event, fmt.format_usage())
        return

    wait_ms = options.wait_ms if options.explicit_wait else int(_cfg("default_wait_ms", 300))

    _ensure_client()
    if _client is None or _client_cfg is None:
        _reply(event, fmt.format_not_configured())
        return

    try:
        output = _exec_with_retry(options.command, wait_ms)
        ctx.log(f"command={options.command!r} output={output!r}")
        output = output if output else "(无输出)"
        output = truncate_text(output, int(_cfg("max_output", 1500)))
        _reply(event, fmt.format_exec_result(options.command, output))
    except RconAuthError:
        _reply(event, fmt.format_auth_failed())
    except Exception as e:
        ctx.log(f"执行失败: {e}", level="error")
        _reply(event, fmt.format_exec_failed())


def handle_add_whitelist(event, match):
    """/添加游戏白名单 <游戏名称>"""
    fmt = MessageFormatter()

    if not _is_enabled():
        _reply(event, fmt.format_not_enabled())
        return

    if not _cfg("whitelist_enabled", True):
        _reply(event, fmt.format_whitelist_not_enabled())
        return

    # 解析游戏名称
    game_name = (match.group(1) or "").strip() if match else ""
    if not game_name:
        game_name = parse_command_args(event.message, "添加游戏白名单") or ""
    if not game_name or not game_name.strip():
        _reply(event, fmt.format_whitelist_usage())
        return

    game_name = game_name.strip()
    if not game_name.replace("_", "").replace("-", "").isalnum():
        _reply(event, "游戏名称只能包含字母、数字、下划线和横线")
        return

    user_id = str(event.user_id)

    # 检查是否已在白名单（独立数据库）
    if _db and _db.is_whitelist_exists(user_id):
        existing_game = _db.get_whitelist_game(user_id) or game_name
        _reply(event, fmt.format_whitelist_already_exists(existing_game))
        return

    # 获取QQ等级（通过 OneBot API）
    try:
        stranger_info = ctx.api("get_stranger_info", user_id=int(user_id), no_cache=True)
    except Exception as e:
        ctx.log(f"获取QQ等级失败: {e}", level="warning")
        stranger_info = None

    qq_level = 0
    if stranger_info and isinstance(stranger_info, dict):
        try:
            qq_level = int(stranger_info.get("level", 0))
        except (ValueError, TypeError):
            qq_level = 0

    # 注意：ctx.api 返回的是原始响应，可能需要从 data 字段取
    # OneBot 11 标准 get_stranger_info 返回 {status, data: {user_id, nickname, level, ...}}
    if isinstance(stranger_info, dict) and "data" in stranger_info:
        data = stranger_info.get("data", {})
        if isinstance(data, dict):
            try:
                qq_level = int(data.get("level", 0))
            except (ValueError, TypeError):
                pass

    min_level = int(_cfg("whitelist_min_level", 15))
    if qq_level < min_level:
        _reply(event, fmt.format_whitelist_level_too_low(qq_level, min_level))
        return

    # 检查入群天数（群聊）
    if event.is_group and event.group_id:
        try:
            member_info = ctx.api("get_group_member_info",
                                  group_id=int(event.group_id),
                                  user_id=int(user_id),
                                  no_cache=True)
            if isinstance(member_info, dict) and "data" in member_info:
                member_info = member_info.get("data", {})
            join_time = int((member_info or {}).get("join_time", 0)) if isinstance(member_info, dict) else 0
            join_days = (int(datetime.datetime.now().timestamp()) - join_time) // 86400 if join_time else 0
        except (ValueError, TypeError):
            join_days = 0

        min_days = int(_cfg("whitelist_min_join_days", 1))
        if join_days < min_days:
            _reply(event, fmt.format_whitelist_join_time_too_short(join_days, min_days))
            return

    # 检查是否被禁言（独立数据库）
    if _db and _db.check_ban(user_id):
        ban_until = _db.get_ban_until(user_id)
        _reply(event, fmt.format_whitelist_banned(ban_until))
        return

    # 执行 MC 命令
    wl_cmd_prefix = _cfg("whitelist_exec_command", "wladd")
    mc_command = f"{wl_cmd_prefix} {game_name}"

    _ensure_client()
    if _client is None or _client_cfg is None:
        _reply(event, fmt.format_not_configured())
        return

    try:
        output = _exec_with_retry(mc_command, 300)
    except RconAuthError:
        _reply(event, fmt.format_auth_failed())
        return
    except Exception as e:
        ctx.log(f"MC指令执行失败: {e}", level="error")
        _reply(event, fmt.format_whitelist_exec_failed())
        return

    # 如果服务器返回"已在白名单"
    if output and "已在白名单" in output:
        _reply(event, "该玩家已在白名单中")
        return

    # 记录到数据库（独立数据库）
    if _db:
        try:
            _db.add_whitelist_record(user_id, game_name)
        except Exception as e:
            ctx.log(f"记录白名单到数据库失败: {e}", level="error")

    _reply(event, f"白名单添加成功！游戏名称：{game_name}")


def handle_force_login(event, match):
    """/强制登录：按QQ匹配白名单中的玩家名，发送 authme forcelogin <玩家>"""
    fmt = MessageFormatter()

    if not _is_enabled():
        _reply(event, fmt.format_not_enabled())
        return

    if not _cfg("forcelogin_enabled", True):
        _reply(event, fmt.format_forcelogin_not_enabled())
        return

    user_id = str(event.user_id)

    # 按QQ查库匹配玩家名（whitelist_records.user_sid -> game_name）
    player_name = None
    if _db:
        try:
            player_name = _db.get_whitelist_game(user_id)
        except Exception as e:
            ctx.log(f"查询玩家绑定失败: {e}", level="error")
    if not player_name or not player_name.strip():
        _reply(event, fmt.format_forcelogin_not_found())
        return
    player_name = player_name.strip()

    # 组装并执行 MC 命令
    # RCON/桥接通道执行命令必须不带前导斜杠，否则会被当成字面命令名报 Unknown command
    cmd_prefix = str(_cfg("forcelogin_exec_command", "authme forcelogin")).strip().lstrip("/")
    mc_command = f"{cmd_prefix} {player_name}"

    _ensure_client()
    if _client is None or _client_cfg is None:
        _reply(event, fmt.format_not_configured())
        return

    try:
        output = _exec_with_retry(mc_command, 300)
        ctx.log(f"强制登录 command={mc_command!r} output={output!r}")
    except RconAuthError:
        _reply(event, fmt.format_auth_failed())
        return
    except Exception as e:
        ctx.log(f"强制登录指令执行失败: {e}", level="error")
        _reply(event, fmt.format_forcelogin_failed())
        return

    _reply(event, fmt.format_forcelogin_success(player_name))


def handle_reset_password(event, match):
    """#重置密码 <新密码>：按QQ匹配绑定玩家，重置其 AuthMe 密码

    场景区分：
    - 私聊：任何人可用，正常流程，直接回复结果
    - 群聊：
      ① 机器人须为群管理员/群主（否则无法撤回消息保护密码，拒绝执行）
      ② 发送者为管理员/群主时机器人无法撤回其消息，拒绝执行
      ③ 普通成员：撤回原消息（含新密码，保护隐私）后执行重置
    回复内容一律不含密码。
    """
    fmt = MessageFormatter()

    if not _is_enabled():
        _reply(event, fmt.format_not_enabled())
        return

    if not _cfg("resetpwd_enabled", True):
        _reply(event, fmt.format_resetpwd_not_enabled())
        return

    # 群聊：双向身份校验（机器人角色 → 发送者角色）
    if event.is_group and event.group_id:
        bot_role = _get_bot_group_role(event.group_id)
        if bot_role not in ("owner", "admin"):
            _reply(event, fmt.format_resetpwd_bot_not_admin())
            return
        if event.role in ("owner", "admin"):
            _reply(event, fmt.format_resetpwd_sender_is_admin())
            return
        # 校验通过：撤回用户原消息（含新密码，保护隐私）
        if event.message_id is not None:
            try:
                ctx.api("delete_msg", message_id=int(event.message_id))
            except Exception as e:
                ctx.log(f"撤回重置密码消息失败: {e}", level="warning")

    # 取新密码参数（第一个 token，密码不允许含空格）
    new_password = (match.group(1) or "").strip() if match else ""
    if not new_password:
        new_password = parse_command_args(event.message, "#重置密码") or ""
    if not new_password:
        new_password = parse_command_args(event.message, "重置密码") or ""
    if not new_password:
        new_password = parse_command_args(event.message, "重设密码") or ""
    if not new_password:
        _reply(event, fmt.format_resetpwd_usage())
        return
    new_password = new_password.split()[0]

    # 密码合法性校验
    if not _valid_password(new_password):
        _reply(event, fmt.format_resetpwd_invalid_password())
        return

    user_id = str(event.user_id)

    # 按QQ查绑定玩家（whitelist_records.user_sid -> game_name）
    player_name = None
    if _db:
        try:
            player_name = _db.get_whitelist_game(user_id)
        except Exception as e:
            ctx.log(f"查询玩家绑定失败: {e}", level="error")
    if not player_name or not player_name.strip():
        _reply(event, fmt.format_resetpwd_not_found())
        return
    player_name = player_name.strip()

    # 组装并执行 MC 命令（RCON/桥接通道必须不带前导斜杠）
    cmd_prefix = str(_cfg("resetpwd_exec_command", "authme password")).strip().lstrip("/")
    mc_command = f"{cmd_prefix} {player_name} {new_password}"

    _ensure_client()
    if _client is None or _client_cfg is None:
        _reply(event, fmt.format_not_configured())
        return

    try:
        output = _exec_with_retry(mc_command, 300)
        ctx.log(f"重置密码 command={mc_command!r} output={output!r}")
    except RconAuthError:
        _reply(event, fmt.format_auth_failed())
        return
    except Exception as e:
        ctx.log(f"重置密码指令执行失败: {e}", level="error")
        _reply(event, fmt.format_resetpwd_failed())
        return

    _reply(event, fmt.format_resetpwd_success())


def handle_force_register(event, match):
    """#强制注册 <游戏ID> <密码>：仅插件管理员可用，代玩家执行 AuthMe 注册并同步写入白名单库。

    - 权限：仅 admins 配置名单内管理员（_is_admin）
    - 无撤回机制：命令与回复原样展示，不调用 delete_msg
    - 密码不写入任何回复内容
    """
    fmt = MessageFormatter()

    if not _is_enabled():
        _reply(event, fmt.format_not_enabled())
        return

    if not _cfg("forcereg_enabled", True):
        _reply(event, fmt.format_forcereg_not_enabled())
        return

    # 仅管理员可用
    if not _is_admin(event.user_id):
        _reply(event, fmt.format_no_permission())
        return

    # 解析参数：游戏ID + 密码（两个 token，密码不允许含空格）
    args = (match.group(1) or "").strip() if match else ""
    if not args:
        args = parse_command_args(event.message, "强制注册") or ""
    tokens = args.split()
    if len(tokens) < 2:
        _reply(event, fmt.format_forcereg_usage())
        return
    if len(tokens) > 2:
        _reply(event, "参数过多：密码不能包含空格，请重试\n" + fmt.format_forcereg_usage())
        return

    game_id, password = tokens[0], tokens[1]

    # 游戏ID合法性校验
    if not game_id.replace("_", "").replace("-", "").isalnum():
        _reply(event, fmt.format_forcereg_invalid_game())
        return

    # 密码合法性校验（4~32 位）
    if not _valid_password(password):
        _reply(event, fmt.format_forcereg_invalid_password())
        return

    # 组装并执行 MC 命令（RCON/桥接通道必须不带前导斜杠）
    cmd_prefix = str(_cfg("forcereg_exec_command", "authme register")).strip().lstrip("/")
    mc_command = f"{cmd_prefix} {game_id} {password}"

    _ensure_client()
    if _client is None or _client_cfg is None:
        _reply(event, fmt.format_not_configured())
        return

    try:
        output = _exec_with_retry(mc_command, 300)
        ctx.log(f"强制注册 command={mc_command!r} output={output!r}")
    except RconAuthError:
        _reply(event, fmt.format_auth_failed())
        return
    except Exception as e:
        ctx.log(f"强制注册指令执行失败: {e}", level="error")
        _reply(event, fmt.format_forcereg_failed())
        return

    # 同步写入白名单库（以管理员QQ作为记录主体，冲突时覆盖）
    db_ok = True
    if _db:
        try:
            _db.upsert_whitelist_record(str(event.user_id), game_id)
        except Exception as e:
            db_ok = False
            ctx.log(f"强制注册写入白名单库失败: {e}", level="error")

    if db_ok:
        _reply(event, fmt.format_forcereg_success(game_id))
    else:
        _reply(event, fmt.format_forcereg_db_failed())


def _valid_password(pwd: str) -> bool:
    """密码合法性校验：4~32 位，仅限字母、数字及常见符号"""
    if not pwd or len(pwd) < 4 or len(pwd) > 32:
        return False
    return all(c.isalnum() or c in "!@#$%^&*._-+=" for c in pwd)


# 机器人群角色缓存：{group_id: (查询时间戳, role)}，60s 过期
_bot_role_cache = {}


def _get_bot_group_role(group_id):
    """查询机器人在群内的角色（owner/admin/member），缓存 60s"""
    now = time.time()
    cached = _bot_role_cache.get(group_id)
    if cached and now - cached[0] < 60:
        return cached[1]

    role = "member"
    try:
        login = ctx.api("get_login_info")
        bot_qq = None
        if isinstance(login, dict):
            data = login.get("data", login)
            bot_qq = str((data or {}).get("user_id") or "")
        if bot_qq:
            info = ctx.api("get_group_member_info",
                           group_id=int(group_id),
                           user_id=int(bot_qq),
                           no_cache=True)
            if isinstance(info, dict) and "data" in info:
                info = info.get("data", {})
            if isinstance(info, dict):
                role = str(info.get("role") or "member")
    except Exception as e:
        ctx.log(f"查询机器人群角色失败: {e}", level="warning")

    _bot_role_cache[group_id] = (time.time(), role)
    return role


# ====================================================================
#  辅助函数
# ====================================================================

def _reply(event, text):
    """统一回复"""
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=text,
    )


def on_unload():
    """插件卸载"""
    global _client, _client_cfg, _db
    _client = None
    _client_cfg = None
    _db = None
    ctx.log("MC控制台插件已卸载")

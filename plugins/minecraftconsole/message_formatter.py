"""消息格式化模块"""

from __future__ import annotations


class MessageFormatter:
    @staticmethod
    def format_exec_result(command: str, output: str) -> str:
        return f"已执行：{command}\n返回：{output}"

    @staticmethod
    def format_no_permission() -> str:
        return "你没有权限使用该指令"

    @staticmethod
    def format_not_enabled() -> str:
        return "插件未启用"

    @staticmethod
    def format_not_configured() -> str:
        return "桥接未配置：请在插件配置中填写 host/port/token（沿用 rcon_* 字段）"

    @staticmethod
    def format_usage() -> str:
        return "用法：/mc-command <MC命令> [--t=5s]"

    @staticmethod
    def format_auth_failed() -> str:
        return "桥接认证失败：请检查 rcon_password 是否与 bridge.token 一致"

    @staticmethod
    def format_exec_failed() -> str:
        return "指令执行失败：请检查桥接插件是否启动、host/port/token 是否正确"

    # ========== 白名单相关 ==========

    @staticmethod
    def format_whitelist_usage() -> str:
        return "用法：添加游戏白名单 <游戏名称>\n示例：添加游戏白名单 Steve"

    @staticmethod
    def format_whitelist_level_too_low(level: int, min_level: int) -> str:
        return (
            f"你的QQ等级为 {level} 级，"
            f"需要达到 {min_level} 级才能添加白名单"
        )

    @staticmethod
    def format_whitelist_join_time_too_short(days: int, min_days: int) -> str:
        return (
            f"你入群仅 {days} 天，"
            f"需要入群满 {min_days} 天才能添加白名单"
        )

    @staticmethod
    def format_whitelist_banned(ban_until: str | None = None) -> str:
        if ban_until:
            return f"你已被禁言，解禁时间：{ban_until}"
        return "你因重复操作已被禁言，请稍后再试"

    @staticmethod
    def format_whitelist_already_exists(game_name: str) -> str:
        return f"你已经添加过白名单了！（{game_name}）"

    @staticmethod
    def format_whitelist_attempt_warning(
        attempts: int, max_attempts: int, ban_hours: int = 24
    ) -> str:
        remaining = max_attempts - attempts
        return (
            f"警告：你已经重复添加了 {attempts} 次，"
            f"如果再尝试 {remaining} 次将被禁言 {ban_hours} 小时"
        )

    @staticmethod
    def format_whitelist_banned_by_attempts(hours: int) -> str:
        return f"你因多次重复添加白名单已被禁言 {hours} 小时"

    @staticmethod
    def format_whitelist_success(game_name: str) -> str:
        return f"白名单添加成功！游戏名称：{game_name}\n请重启游戏后进入服务器"

    @staticmethod
    def format_whitelist_exec_failed() -> str:
        return "白名单添加失败：MC服务器指令执行出错，请联系管理员"

    @staticmethod
    def format_whitelist_not_enabled() -> str:
        return "白名单功能未启用"

    @staticmethod
    def format_whitelist_db_error() -> str:
        return "系统内部错误，请联系管理员"

    @staticmethod
    def format_whitelist_no_group_info() -> str:
        return "无法获取群成员信息，请在群聊中使用此命令"

    # ========== 强制登录相关 ==========

    @staticmethod
    def format_forcelogin_usage() -> str:
        return "用法：强制登录"

    @staticmethod
    def format_forcelogin_not_enabled() -> str:
        return "强制登录功能未启用"

    @staticmethod
    def format_forcelogin_not_found() -> str:
        return "未找到绑定玩家，请先发送 添加游戏白名单 <游戏名>"

    @staticmethod
    def format_forcelogin_success(player_name: str) -> str:
        return f"强制登录指令已发送：{player_name}\n请稍候片刻，若仍未进入游戏请联系管理员"

    @staticmethod
    def format_forcelogin_failed() -> str:
        return "强制登录失败：MC服务器指令执行出错，请联系管理员"
    # ========== 重置密码相关 ==========

    @staticmethod
    def format_resetpwd_usage() -> str:
        return "用法：#重置密码 <新密码>\n示例：#重置密码 abc123456"

    @staticmethod
    def format_resetpwd_not_enabled() -> str:
        return "重置密码功能未启用"

    @staticmethod
    def format_resetpwd_not_found() -> str:
        return "未找到绑定玩家，请先发送 添加游戏白名单 <游戏名>"

    @staticmethod
    def format_resetpwd_invalid_password() -> str:
        return "密码格式不正确：需 4~32 位，仅限字母、数字及常见符号，且不能包含空格"

    @staticmethod
    def format_resetpwd_bot_not_admin() -> str:
        return "机器人不是本群管理员，无法撤回消息保护密码，群内重置密码不可用。请私聊机器人操作"

    @staticmethod
    def format_resetpwd_sender_is_admin() -> str:
        return "你是管理员/群主，机器人无法撤回你的消息，所以不会重置密码。请私聊机器人操作"

    @staticmethod
    def format_resetpwd_success() -> str:
        return "密码重置成功！新密码已生效，请用新密码登录游戏"

    @staticmethod
    def format_resetpwd_failed() -> str:
        return "密码重置失败：MC服务器指令执行出错，请联系管理员"

    # ========== 强制注册相关 ==========

    @staticmethod
    def format_forcereg_usage() -> str:
        return "用法：#强制注册 <游戏ID> <密码>\n示例：#强制注册 Steve abc123456"

    @staticmethod
    def format_forcereg_not_enabled() -> str:
        return "强制注册功能未启用"

    @staticmethod
    def format_forcereg_invalid_game() -> str:
        return "游戏ID不合法：仅限字母、数字、下划线和横线"

    @staticmethod
    def format_forcereg_invalid_password() -> str:
        return "密码格式不正确：需 4~32 位，仅限字母、数字及常见符号，且不能包含空格"

    @staticmethod
    def format_forcereg_success(game_name: str) -> str:
        return f"强制注册成功！游戏ID：{game_name}\n已执行 AuthMe 注册并写入白名单库"

    @staticmethod
    def format_forcereg_failed() -> str:
        return "强制注册失败：MC服务器指令执行出错，请联系管理员"

    @staticmethod
    def format_forcereg_db_failed() -> str:
        return "强制注册成功，但白名单库写入失败，请联系管理员检查数据库"

"""
qqadmin 群管插件 - 工具函数与按群配置存储

配置存储：data/plugins_dat/qqadmin/configs.json
结构：
    {"default": {...}, "<group_id>": {...}}
读取时具体群配置继承 default（未配置的键回退到默认值）。
"""
import json
import os
import re
import time

# 内置违禁词（精简版，可在 Web UI / 群内命令中覆盖）
BUILTIN_BAN_WORDS = [
    "傻逼", "傻b", "煞笔", "sb", "脑残", "智障", "白痴", "弱智", "废物",
    "滚粗", "去死", "找死", "你妈", "妈的", "操你", "草泥马", "卧槽尼玛",
    "狗东西", "垃圾玩意", "贱人", "婊子", "妓女", "龟儿子", "王八蛋",
]

# 各群默认配置模板（default 群 + 具体群共享同一结构）
DEFAULT_CONFIG = {
    "ban_time": 600,                # 默认禁言秒数（无参数时）
    "max_ban_time": 2592000,        # 禁言秒数上限（30天）
    "random_ban_time": False,       # 禁言时间随机 1-该值
    "ban_word_enabled": False,      # 违禁词检测总开关
    "ban_word_list": "",            # 自定义违禁词（逗号分隔，追加到内置）
    "ban_word_ban_time": 60,        # 命中违禁词禁言秒数
    "builtin_ban_words": True,      # 使用内置违禁词库
    "ban_word_mode": "local",       # 违禁词模式: local/api/both
    "ban_word_api_url": "https://api-v2.yuafeng.cn/API/wjc.php",
    "spam_enabled": False,          # 刷屏检测开关
    "spam_threshold": 5,            # 刷屏触发条数（窗口内）
    "spam_window": 5,               # 刷屏检测窗口（秒）
    "spam_ban_time": 300,           # 刷屏禁言秒数
    "curfew_enabled": False,        # 宵禁开关
    "curfew_start": "23:00",        # 宵禁开始 HH:MM
    "curfew_end": "06:00",          # 宵禁结束 HH:MM
    "join_welcome": True,           # 进群欢迎（默认开）
    "join_welcome_msg": "欢迎新成员 {name} 加入本群！",
    "join_ban_time": 0,             # 进群禁言秒数（0=关闭）
    "leave_notify": False,          # 退群通知
    "leave_blacklist": False,       # 退群自动拉黑（下次进群拒绝）
    "join_review": False,           # 进群审核总开关
    "join_accept_words": "",        # 自动批准关键词（空格分隔）
    "join_reject_words": "",        # 自动拒绝关键词（空格分隔）
    "join_no_match_reject": False,  # 未命中白词自动驳回
    "join_min_level": 0,            # 进群等级门槛（0=不限）
    "join_max_times": 3,            # 未命中尝试次数上限（0=不限）
    "join_blacklist": "",           # 进群黑名单 QQ（空格分隔）
    "assistants": "",               # 协管名单（空格分隔 QQ）
}

# 配置项中文标签映射（用于群内配置查看/设置）
LABEL_MAP = {
    "ban_time": "默认禁言秒数",
    "max_ban_time": "禁言上限秒数",
    "random_ban_time": "随机禁言秒数(0=关)",
    "ban_word_enabled": "违禁词检测",
    "ban_word_ban_time": "违禁词禁言秒数",
    "builtin_ban_words": "内置禁词",
    "ban_word_mode": "违禁词模式",
    "ban_word_api_url": "违禁词API地址",
    "spam_enabled": "刷屏检测",
    "spam_threshold": "刷屏条数",
    "spam_window": "刷屏窗口秒",
    "spam_ban_time": "刷屏禁言秒数",
    "curfew_enabled": "宵禁",
    "curfew_start": "宵禁开始",
    "curfew_end": "宵禁结束",
    "join_welcome": "进群欢迎",
    "join_ban_time": "进群禁言秒数",
    "leave_notify": "退群通知",
    "leave_blacklist": "退群拉黑",
    "join_review": "进群审核",
    "join_no_match_reject": "未命中驳回",
    "assistants": "协管名单",
}

# 中文→key 反查映射
LABEL_REVERSE = {v: k for k, v in LABEL_MAP.items()}


class GroupConfigStore:
    """按群配置存储（JSON 文件，进程间持久）"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.path = os.path.join(ctx.get_data_dir(), "configs.json")
        self._cache = None
        self._lock = None
        try:
            import threading
            self._lock = threading.Lock()
        except Exception:
            pass

    def _load(self) -> dict:
        if self._cache is not None:
            return self._cache
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._cache = data
                    return data
        except Exception as e:
            print(f"[qqadmin] 读取配置失败: {e}")
        self._cache = {"default": dict(DEFAULT_CONFIG)}
        return self._cache

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[qqadmin] 保存配置失败: {e}")

    def get_default(self) -> dict:
        data = self._load()
        d = data.get("default")
        if d is None:
            d = dict(DEFAULT_CONFIG)
            data["default"] = d
        merged = dict(DEFAULT_CONFIG)
        merged.update(d or {})
        return merged

    def get_group(self, gid) -> dict:
        """获取某群配置（继承 default）"""
        default = self.get_default()
        data = self._load()
        g = data.get(str(gid), {}) or {}
        merged = dict(default)
        merged.update(g)
        return merged

    def set_group(self, gid, key, value):
        """设置某群配置项（值等于 default 时删除该群覆盖，保持跟随默认）"""
        data = self._load()
        default = self.get_default()
        key = str(key)
        if str(gid) == "default":
            if default.get(key) == value:
                return  # 无变化
            data["default"][key] = value
        else:
            g = data.setdefault(str(gid), {})
            if default.get(key) == value:
                g.pop(key, None)
            else:
                g[key] = value
        self._save()

    def reset_group(self, gid):
        """重置某群配置（跟随默认）"""
        data = self._load()
        if str(gid) == "all":
            data.clear()
            data["default"] = dict(DEFAULT_CONFIG)
        else:
            data.pop(str(gid), None)
        self._save()

    def all_group_ids(self) -> list:
        """返回所有配置了独立设置的群号列表（不含 default）"""
        data = self._load()
        return [str(g) for g in data.keys() if str(g) != "default"]

    def get_cn_lines(self, gid) -> str:
        """导出配置为中文行文本"""
        cfg = self.get_group(gid)
        lines = []
        for k, label in LABEL_MAP.items():
            v = cfg.get(k)
            if v is None:
                continue
            if isinstance(v, bool):
                v = "开" if v else "关"
            # 协管名单只显示计数
            if k == "assistants" and isinstance(v, str):
                qq_list = [q for q in v.split() if q]
                v = f"{len(qq_list)} 人"
            lines.append(f"{label}: {v}")
        return "\n".join(lines)

    def import_cn_lines(self, gid, raw: str):
        """从中文配置行文本导入"""
        for line in raw.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            label, val = line.split(":", 1)
            label = label.strip()
            val = val.strip()
            key = LABEL_REVERSE.get(label)
            if not key:
                continue
            cfg = self.get_group(gid)
            cur = cfg.get(key)
            if isinstance(cur, bool):
                value = val in ("开", "on", "true", "1", "是")
            elif isinstance(cur, int):
                try:
                    value = int(float(val))
                except ValueError:
                    continue
            else:
                value = val
            self.set_group(gid, key, value)


# ===================== 通用工具 =====================


def get_ats(event) -> list:
    """获取被 @ 的 QQ 列表（含数字校验）"""
    ats = []
    for uid in (event.at_list or []):
        if uid and str(uid).isdigit():
            ats.append(int(uid))
    return ats


def get_reply_id(event):
    """获取引用的消息 ID"""
    return event.reply_id


def get_image_url(event):
    """获取消息中的图片 URL"""
    try:
        img = event.first_image
        if img:
            return img.get("url") or img.get("file") or ""
    except Exception:
        pass
    return ""


def get_nickname(ctx, gid, uid) -> str:
    """获取群成员昵称/群名片"""
    try:
        info = ctx.get_member_info(gid, uid)
        if info:
            return info.get("card") or info.get("nickname") or str(uid)
    except Exception:
        pass
    return str(uid)


def get_ban_words(cfg) -> list:
    """合并内置 + 自定义违禁词"""
    words = []
    if cfg.get("builtin_ban_words", True):
        words.extend(BUILTIN_BAN_WORDS)
    raw = cfg.get("ban_word_list", "") or ""
    words.extend(w.strip() for w in raw.replace("，", ",").split(",") if w.strip())
    return words


def find_ban_words(text: str, words: list) -> list:
    """返回命中的违禁词列表（去重，保持顺序）"""
    hit = []
    seen = set()
    for w in words:
        if w and w in text and w not in seen:
            hit.append(w)
            seen.add(w)
    return hit


def mask_text(text: str, words: list) -> str:
    """把命中词替换为 **"""
    for w in words:
        text = text.replace(w, "*" * len(w))
    return text


def parse_time_range(s: str):
    """解析 'HH:MM HH:MM' -> (start_min, end_min) 或 None"""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s+(\d{1,2}):(\d{2})\s*$", s)
    if not m:
        return None
    sh, sm, eh, em = map(int, m.groups())
    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        return None
    return sh * 60 + sm, eh * 60 + em


def in_time_window(now_min: int, start_min: int, end_min: int) -> bool:
    """判断当前分钟是否在窗口内（支持跨天）"""
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= now_min < end_min
    # 跨天
    return now_min >= start_min or now_min < end_min


def level_rank(role: str) -> int:
    """角色等级：super4 > owner3 > admin2 > member1 > blacklist0"""
    return {"super": 4, "owner": 3, "admin": 2, "member": 1, "blacklist": 0}.get(role, 0)


def has_perm(event, level: str) -> bool:
    """event 是否达到指定权限等级（super/owner/admin/member）"""
    return level_rank(event.role) >= level_rank(level)


def seconds_to_text(sec: int) -> str:
    """秒数转可读文本"""
    sec = int(sec)
    if sec <= 0:
        return "0秒"
    if sec < 60:
        return f"{sec}秒"
    if sec < 3600:
        return f"{sec // 60}分钟"
    if sec < 86400:
        return f"{sec // 3600}小时"
    return f"{sec // 86400}天"


# ===================== 协管名单管理 =====================


def get_assistants(cfg) -> list:
    """获取群协管 QQ 列表"""
    raw = cfg.get("assistants", "") or ""
    return [q for q in raw.split() if q.isdigit()]


def is_assistant(cfg, uid) -> bool:
    """判断 QQ 是否为本群协管"""
    return str(uid) in (cfg.get("assistants") or "").split()


def add_assistant(store, gid, uid):
    """添加协管，返回是否新增"""
    cur = store.get_group(gid).get("assistants", "") or ""
    lst = cur.split()
    s = str(uid)
    if s in lst:
        return False
    lst.append(s)
    store.set_group(gid, "assistants", " ".join(lst))
    return True


def remove_assistant(store, gid, uid):
    """移除协管，返回是否存在"""
    cur = store.get_group(gid).get("assistants", "") or ""
    lst = [q for q in cur.split() if q != str(uid)]
    store.set_group(gid, "assistants", " ".join(lst))
    return str(uid) in cur.split()
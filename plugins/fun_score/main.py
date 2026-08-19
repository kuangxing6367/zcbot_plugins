"""
签到积分插件主入口（zgric_onebot11 新插件语法）
================================================

复刻自 astrbot_plugin_fun_score，补回完整 Pillow 图片渲染功能。

功能：
- /签到 [0-3]            每日签到，经验+1（上限max_exp），金币=1+random(1,base_coins)+rank*5
- /积分 /我的积分         查看自己当前经验/金币/等级
- /排行榜 /积分排行 /查看等级排名 /签到排名  等级排名柱状图（图片）
- /等级                  查看等级体系（纯文本）
- /获得签到背景           获取今日签到背景图
- /设置签到预设 [0-3]     设置群默认签到样式（管理员）
- 违禁词检测             订阅 message 事件，命中后打码 + 撤回 + 禁言
- LLM 工具               sign_in / view_score_rank（通过 plugin_llm_core 注册）
- 用户同步               sid -> uid 映射自动写入 fun_user 表

设计说明：
- ctx 由框架在 register(ctx) 调用前注入为模块全局变量，handler 内可直接使用
- 数据存于 fun_score 表（user_id + group_id 联合主键），框架自带数据库 ctx.db_*
- 图片通过临时 PNG 文件 + CQ 码 [CQ:image,file=file:///路径] 发送，发送后立即删除
- 图片生成失败时回退纯文本
- 等级表 rankArray = [0,10,20,50,100,200,350,550,750,1000,1200]
- LLM 工具通过 sys.modules.get('plugin_llm_core') 跨插件注册
"""
import datetime
import os
import random
import sys
import tempfile

# 将插件目录加入 sys.path，确保 core 模块可被直接 import（框架以插件目录为 cwd 加载 main.py）
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from core.score import ScoreCore, get_rank, rankArray
from ban_word import BanWordHandle
from db import ScoreDB, UserDB

__plugin_meta__ = {
    "name": "签到积分",
    "version": "2.1.0",
    "author": "zgric",
    "desc": "每日签到/积分/排行榜/等级/违禁词检测/LLM工具（Pillow 图片渲染版）",
    "priority": 30,
}

# 防止心跳重注册建表
_table_created = False
# 防止心跳重复订阅 message 事件
_message_subscribed = False

_CREATE_FUN_SCORE = """
CREATE TABLE IF NOT EXISTS fun_score (
    user_id BIGINT NOT NULL,
    group_id BIGINT NOT NULL DEFAULT 0,
    score INT DEFAULT 0 COMMENT '经验值',
    gold INT DEFAULT 0 COMMENT '金币余额',
    level INT DEFAULT 0 COMMENT '等级(0-10)',
    sign_count INT DEFAULT 0 COMMENT '累计签到次数',
    last_sign_date DATE DEFAULT NULL,
    PRIMARY KEY (user_id, group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_CREATE_FUN_SCORE_STYLE = """
CREATE TABLE IF NOT EXISTS fun_score_style (
    group_id BIGINT PRIMARY KEY,
    style INT DEFAULT 1
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_CREATE_FUN_USER = """
CREATE TABLE IF NOT EXISTS fun_user (
    sid VARCHAR(50) NOT NULL COMMENT '平台用户ID(QQ号)',
    uid VARCHAR(100) DEFAULT NULL COMMENT '用户昵称',
    uuid VARCHAR(36) DEFAULT NULL COMMENT '内部唯一标识',
    first_seen INT DEFAULT NULL COMMENT '首次出现时间戳',
    last_seen INT DEFAULT NULL COMMENT '最后出现时间戳',
    score INT DEFAULT 0 COMMENT '积分',
    gold INT DEFAULT 0 COMMENT '金币余额',
    sign_count INT DEFAULT 0 COMMENT '累计签到次数',
    last_sign_date DATE DEFAULT NULL COMMENT '最后签到日期',
    PRIMARY KEY (sid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='签到用户数据表'
"""

# 绘制核心（延迟初始化，需要 ctx 配置）
_core = None

# 违禁词检测处理器
_ban_word_handler = None

# 数据库操作实例
_score_db = None
_user_db = None


def register(ctx):
    """插件注册入口"""
    global _table_created, _core, _message_subscribed
    global _ban_word_handler, _score_db, _user_db

    if not _table_created:
        try:
            ctx.db_execute(_CREATE_FUN_SCORE)
            ctx.db_execute(_CREATE_FUN_SCORE_STYLE)
            ctx.db_execute(_CREATE_FUN_USER)
            _table_created = True
        except Exception as e:
            ctx.log(f"创建表失败: {e}", level="warning")

    # 初始化数据库操作实例
    _score_db = ScoreDB(ctx)
    _user_db = UserDB(ctx)
    # 确保 fun_user 表有签到相关字段
    _score_db.init_tables()

    # 初始化违禁词检测处理器
    _ban_word_handler = BanWordHandle(ctx)

    # 初始化签到渲染器单例（渲染走 image_renderer 的 Canvas 接口，无需注册到其注册表）
    global _core
    try:
        img_mod = sys.modules.get("plugin_image_renderer")
        if img_mod is not None and hasattr(img_mod, "_get_native_or_pil_canvas"):
            _core = ScoreCore({
                "bg_api": _bg_api(),
                "max_exp": _max_exp(),
                "base_coins": _base_coins(),
            })
            ctx.log("签到渲染器已初始化（走 image_renderer Canvas）", level="info")
        else:
            ctx.log("image_renderer 未加载，签到图片将回退纯文本", level="warning")
    except Exception as e:
        ctx.log(f"初始化签到渲染器失败: {e}", level="warning")

    ctx.command("签到", handle_sign, priority=30, description="每日签到（经验+1，金币随机）")
    ctx.command("积分|我的积分", handle_score, priority=30, description="查看自己的经验/金币/等级")
    ctx.command("排行榜|积分排行|查看等级排名|签到排名", handle_rank, priority=30, description="等级排行榜（图片）")
    ctx.command("等级", handle_level, priority=30, description="查看等级体系信息")
    ctx.command("获得签到背景", handle_get_bg, priority=30, description="获取签到背景图")
    ctx.command("设置签到预设", handle_set_style, priority=30, require_admin=True, description="设置群默认签到样式（管理员）")

    # 订阅消息事件做违禁词检测（避免重复订阅）
    if not _message_subscribed:
        ctx.on("message", on_message)
        _message_subscribed = True

    # 注册 LLM 工具
    _register_llm_tools()

    ctx.log("签到积分插件已加载（图片渲染版 + 违禁词检测 + LLM工具）")


# ====================================================================
#  配置读取辅助
# ====================================================================

def _max_exp():
    return int(ctx.get_config("max_exp", 1200))


def _base_coins():
    return int(ctx.get_config("base_coins", 10))


def _bg_api():
    return ctx.get_config("bg_api", "https://furry.axzt.top/")


# ====================================================================
#  统一回复封装
# ====================================================================

def _reply(event, text):
    """群聊回群、私聊回私"""
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=text,
    )


def _gid(event):
    """获取群号；私聊时用 0 占位（与表 group_id 默认值一致）"""
    return event.group_id if event.is_group and event.group_id else 0


def _nickname(event):
    """获取昵称，优先群成员信息，否则用 sender_nickname，最后用 user_id"""
    uid = event.user_id
    try:
        nick = event.sender_nickname
        if nick:
            return nick
    except Exception:
        pass
    gid = _gid(event)
    if gid:
        try:
            info = ctx.get_member_info(gid, uid)
            if info:
                return info.get("card") or info.get("nickname") or str(uid)
        except Exception:
            pass
    return str(uid)


def _send_image(event, image_bytes):
    """保存图片到临时文件，发送CQ码图片，然后删除临时文件"""
    if not image_bytes:
        return False
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(image_bytes)
            tmp_path = f.name
        # Windows 路径需要用正斜杠
        path_str = tmp_path.replace("\\", "/")
        cq = f"[CQ:image,file=file:///{path_str}]"
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message=cq,
        )
        return True
    except Exception as e:
        ctx.log(f"发送图片失败: {e}", level="warning")
        return False
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _send_image_path(event, path):
    """通过文件路径发送 CQ 图片"""
    try:
        path_str = path.replace("\\", "/")
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message=f"[CQ:image,file=file:///{path_str}]",
        )
        return True
    except Exception as e:
        ctx.log(f"发送图片失败: {e}", level="warning")
        return False


# ====================================================================
#  签到数据读写
# ====================================================================

def _get_user(uid, gid):
    """读取用户签到记录，返回 dict 或 None"""
    try:
        return ctx.db_query_one(
            "SELECT user_id, group_id, score, gold, level, sign_count, last_sign_date "
            "FROM fun_score WHERE user_id=%s AND group_id=%s",
            (int(uid), int(gid)),
        )
    except Exception as e:
        ctx.log(f"读取用户记录失败 uid={uid} gid={gid}: {e}", level="warning")
        return None


def _ensure_user(uid, gid):
    """确保用户记录存在，不存在则插入，返回记录 dict"""
    row = _get_user(uid, gid)
    if row is not None:
        return row
    try:
        ctx.db_execute(
            "INSERT INTO fun_score (user_id, group_id, score, gold, level, sign_count, last_sign_date) "
            "VALUES (%s, %s, 0, 0, 0, 0, NULL)",
            (int(uid), int(gid)),
        )
    except Exception as e:
        ctx.log(f"插入用户记录失败 uid={uid} gid={gid}: {e}", level="warning")
    return _get_user(uid, gid)


def _parse_last_date(last):
    """统一把 last_sign_date 转成 date 对象或 None"""
    if last is None:
        return None
    if isinstance(last, datetime.datetime):
        return last.date()
    if isinstance(last, datetime.date):
        return last
    if isinstance(last, str):
        try:
            return datetime.date.fromisoformat(last[:10])
        except Exception:
            return None
    return None


# ====================================================================
#  群默认样式
# ====================================================================

def _get_group_style(gid):
    """获取群默认签到样式，默认1"""
    if not gid:
        return 1
    try:
        row = ctx.db_query_one(
            "SELECT style FROM fun_score_style WHERE group_id=%s",
            (int(gid),),
        )
        return int(row.get("style", 1)) if row else 1
    except Exception:
        return 1


def _set_group_style(gid, style):
    """设置群默认签到样式"""
    ctx.db_execute(
        "INSERT INTO fun_score_style (group_id, style) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE style=VALUES(style)",
        (int(gid), int(style)),
    )


# ====================================================================
#  今日签到统计
# ====================================================================

def _get_today_sign_rank(uid, gid):
    """获取今日签到排名：总签到人数 + 当前用户排名"""
    today = datetime.date.today()
    try:
        # 今日总签到人数（同群）
        row = ctx.db_query_one(
            "SELECT COUNT(*) AS c FROM fun_score "
            "WHERE group_id=%s AND last_sign_date=%s",
            (int(gid), today),
        )
        total = int(row.get("c", 0)) if row else 0
    except Exception:
        total = 0
    try:
        # 当前用户排名：按 sign_count 排序，签到次数少的靠前（先签到）
        row = ctx.db_query_one(
            "SELECT COUNT(*)+1 AS r FROM fun_score "
            "WHERE group_id=%s AND last_sign_date=%s "
            "AND sign_count < (SELECT sign_count FROM fun_score "
            "WHERE user_id=%s AND group_id=%s AND last_sign_date=%s)",
            (int(gid), today, int(uid), int(gid), today),
        )
        rank = int(row.get("r", 0)) if row else 0
    except Exception:
        rank = 0
    return {"total": total, "rank": rank}


def _get_rank_list(gid, top_n=10):
    """获取等级排名列表（前N名），返回 [{uid,score,nickname}]"""
    try:
        rows = ctx.db_query(
            "SELECT user_id, score FROM fun_score "
            "WHERE group_id=%s AND score>0 "
            "ORDER BY score DESC LIMIT %s",
            (int(gid), int(top_n)),
        )
    except Exception as e:
        ctx.log(f"获取排行榜失败: {e}", level="warning")
        return []
    result = []
    for r in rows:
        u = r.get("user_id")
        score = int(r.get("score") or 0)
        nick = str(u)
        if gid:
            try:
                info = ctx.get_member_info(gid, u)
                if info:
                    nick = info.get("card") or info.get("nickname") or str(u)
            except Exception:
                pass
        result.append({"uid": u, "score": score, "nickname": nick})
    return result


# ====================================================================
#  签到图片生成（封装失败回退）
# ====================================================================

def _ensure_signin_renderer(img_mod):
    """确保签到渲染器（ScoreCore）可用，懒初始化单例。

    image_renderer 官方版只提供 Canvas 渲染接口（_get_native_or_pil_canvas /
    image_circle_crop 等），没有 register_renderer/get_renderer 注册表；
    这里直接持有 ScoreCore 单例，渲染时其内部调用 image_renderer 的 Canvas 完成绘制。
    """
    global _core
    if img_mod is None or not hasattr(img_mod, "_get_native_or_pil_canvas"):
        return None
    try:
        if _core is None:
            _core = ScoreCore({
                "bg_api": _bg_api(),
                "max_exp": _max_exp(),
                "base_coins": _base_coins(),
            })
            ctx.log("签到渲染器（ScoreCore）已懒初始化", level="info")
        return _core
    except Exception as e:
        ctx.log(f"初始化签到渲染器失败: {e}", level="warning")
        return None


def _render_sign(event, data, style):
    """生成签到图片并发送（渲染走 image_renderer Canvas），失败返回 False 则回退纯文本"""
    mod = sys.modules.get("plugin_image_renderer")
    if mod is None:
        return False
    try:
        renderer = _ensure_signin_renderer(mod)
        if renderer is None:
            return False
        image_bytes = renderer.render_sign_image(data, style)
        if not image_bytes:
            return False
        return _send_image(event, image_bytes)
    except Exception as e:
        ctx.log(f"签到图渲染异常: {e}", level="warning")
        return False


def _render_rank(event, rank_list):
    """生成排行榜图片并发送（渲染走 image_renderer render_list），失败返回 False"""
    mod = sys.modules.get("plugin_image_renderer")
    if mod is None:
        return False
    try:
        # 排行榜走 image_renderer._render_list_image 榜单渲染（序号/名称/数值，前三名高亮）
        items = []
        for i, r in enumerate(rank_list):
            rank = i + 1
            items.append({
                "name": str(r.get("nickname", str(r.get("uid", "")))),
                "value": str(r.get("score", 0)),
                "rank": rank,
                "highlight": rank <= 3,
            })
        options = {
            "item_size": 20,
            "title_size": 26,
            "name_color": (40, 40, 60, 255),
            "value_color": (99, 102, 241, 255),
            "highlight_bg": (236, 239, 255, 255),
            "highlight_color": (99, 102, 241, 255),
            "rank_color": (160, 160, 170, 255),
            "radius": 16,
            "border_color": (224, 226, 240, 255),
            "border_width": 2,
            "bg_gradient": [(248, 250, 255, 255), (255, 255, 245, 255)],
        }
        scope = "本群" if _gid(event) else "全局"
        image = mod._render_list_image(
            f"{scope}等级排行榜 TOP{len(rank_list)}", items, 640, 30, options
        )
        if image is None:
            return False
        # 原生返回 PNG bytes，PIL 回退返回 Image，统一转 bytes
        if isinstance(image, (bytes, bytearray)):
            image_bytes = bytes(image)
        else:
            import io as _io
            buf = _io.BytesIO()
            image.save(buf, format="PNG")
            image_bytes = buf.getvalue()
        return _send_image(event, image_bytes)
    except Exception as e:
        ctx.log(f"排行榜图渲染异常: {e}", level="warning")
        return False


# ====================================================================
#  命令处理
# ====================================================================

def handle_sign(event, match):
    """每日签到：签到 ([0-3])"""
    uid = event.user_id
    gid = _gid(event)
    nickname = _nickname(event)
    today = datetime.date.today()

    # 解析样式参数
    style = None
    try:
        if match and match.groups():
            arg = (match.group(1) or "").strip()
            if arg != "":
                style = int(arg)
    except Exception:
        style = None
    # 未指定样式则跟随群默认
    if style is None:
        style = _get_group_style(gid)
    # 样式范围校验
    if style < 0 or style > 3:
        _reply(event, "签到样式编号应为 0-3")
        return

    row = _ensure_user(uid, gid)
    if row is None:
        _reply(event, "签到失败：无法读取用户数据，请稍后再试。")
        return

    last = _parse_last_date(row.get("last_sign_date"))
    sign_count = int(row.get("sign_count") or 0)
    max_exp = _max_exp()
    base_coins = _base_coins()

    # 已签到：渲染当前状态卡片
    if last is not None and last == today:
        exp = int(row.get("score") or 0)
        gold = int(row.get("gold") or 0)
        rank = get_rank(exp)
        sign_rank = _get_today_sign_rank(uid, gid)
        data = {
            "uid": uid, "nickname": nickname, "inc": 0, "gold": gold,
            "exp": exp, "rank": rank,
            "sign_rank_today": sign_rank["rank"],
            "total_sign_today": sign_rank["total"],
            "total_sign_days": sign_count,
        }
        ok = _render_sign(event, data, style)
        if not ok:
            _reply(event,
                   f"{nickname} 今天已经签到过了~\n"
                   f"经验值：{exp}/{max_exp}  等级：LV{rank}\n"
                   f"金币余额：{gold}\n"
                   f"累计签到：{sign_count} 天\n"
                   f"明天再来吧！")
        return

    # 未签到：执行签到
    # 跨天重置签到次数
    if last is None or last != today:
        # 若上次签到不是昨天，累计天数清零（按 sign_count 处理：last==None时为0，跨天则保留+1）
        pass

    # 计算新经验 +1（上限 max_exp）
    old_exp = int(row.get("score") or 0)
    new_exp = old_exp + 1
    if new_exp > max_exp:
        new_exp = max_exp
    rank = get_rank(new_exp)

    # 计算金币 = 1 + random(1, base_coins) + rank*5
    inc = 1 + random.randint(1, base_coins) + rank * 5
    old_gold = int(row.get("gold") or 0)
    new_gold = old_gold + inc

    # 累计签到次数 +1
    new_sign_count = sign_count + 1

    # 写回数据库
    try:
        ctx.db_execute(
            "INSERT INTO fun_score (user_id, group_id, score, gold, level, sign_count, last_sign_date) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE score=VALUES(score), gold=VALUES(gold), "
            "level=VALUES(level), sign_count=VALUES(sign_count), last_sign_date=VALUES(last_sign_date)",
            (int(uid), int(gid), new_exp, new_gold, rank, new_sign_count, today),
        )
    except Exception as e:
        _reply(event, f"签到失败：写入数据出错 - {e}")
        return

    # 获取今日签到排名
    sign_rank = _get_today_sign_rank(uid, gid)

    data = {
        "uid": uid, "nickname": nickname, "inc": inc, "gold": new_gold,
        "exp": new_exp, "rank": rank,
        "sign_rank_today": sign_rank["rank"],
        "total_sign_today": sign_rank["total"],
        "total_sign_days": new_sign_count,
    }

    ok = _render_sign(event, data, style)
    if not ok:
        # 回退纯文本
        msg = "签到成功"
        if new_exp >= max_exp:
            msg += "，你的经验已经达到上限"
        _reply(event,
               f"=== {msg} ===\n"
               f"{nickname}\n"
               f"金币 + {inc}  当前金币：{new_gold}\n"
               f"经验 +1  当前经验：{new_exp}/{max_exp}\n"
               f"等级：LV{rank}\n"
               f"累计签到：{new_sign_count} 天\n"
               f"今日第 {sign_rank['rank']} 个签到（共 {sign_rank['total']} 人）")


def handle_score(event, match):
    """查看自己的经验/金币/等级"""
    uid = event.user_id
    gid = _gid(event)
    nickname = _nickname(event)

    row = _get_user(uid, gid)
    if row is None:
        _reply(event, f"{nickname} 还没有签到记录，发送「签到」开始你的积分之旅吧~")
        return

    exp = int(row.get("score") or 0)
    gold = int(row.get("gold") or 0)
    rank = get_rank(exp)
    sign_count = int(row.get("sign_count") or 0)
    last = _parse_last_date(row.get("last_sign_date"))
    max_exp = _max_exp()
    nextrank = rankArray[rank + 1] if rank < 10 else rankArray[-1]

    _reply(event,
           f"=== 我的积分 ===\n"
           f"昵称：{nickname}\n"
           f"经验值：{exp}/{max_exp}  ({exp}/{nextrank} 下一档)\n"
           f"等级：LV{rank}\n"
           f"金币余额：{gold}\n"
           f"累计签到：{sign_count} 天\n"
           f"最后签到：{last if last else '未签到'}")


def handle_rank(event, match):
    """等级排行榜（图片，失败回退文本）"""
    gid = _gid(event)
    rank_list = _get_rank_list(gid, 10)

    if not rank_list:
        _reply(event, "暂无积分排行数据，快来成为第一名吧！")
        return

    ok = _render_rank(event, rank_list)
    if not ok:
        # 回退纯文本排行榜
        scope = "本群" if gid else "全局"
        lines = [f"=== {scope}等级排行榜 TOP{len(rank_list)} ==="]
        medals = ["[1]", "[2]", "[3]"]
        for i, r in enumerate(rank_list):
            rank = i + 1
            prefix = medals[i] if i < 3 else f"[{rank}]"
            lines.append(f"{prefix} {r['nickname']}  经验:{r['score']}  LV{get_rank(r['score'])}")
        lines.append("=" * 20)
        _reply(event, "\n".join(lines))


def handle_level(event, match):
    """查看等级体系信息"""
    uid = event.user_id
    gid = _gid(event)
    row = _get_user(uid, gid)
    my_exp = int(row.get("score") or 0) if row else 0
    my_rank = get_rank(my_exp)

    lines = ["=== 等级体系 ==="]
    for i, val in enumerate(rankArray):
        next_val = rankArray[i + 1] if i < len(rankArray) - 1 else None
        mark = " <- 你在这里" if i == my_rank else ""
        if next_val is None:
            lines.append(f"LV{i}  {val}+ 经验{mark}")
        else:
            lines.append(f"LV{i}  {val}-{next_val - 1} 经验{mark}")
    lines.append("=" * 20)
    lines.append(f"你的经验：{my_exp}  等级：LV{my_rank}")
    _reply(event, "\n".join(lines))


def handle_get_bg(event, match):
    """获取签到背景图"""
    from core.score import _download_image
    import io
    data = _download_image(_bg_api())
    if data is None:
        _reply(event, f"签到背景图API地址：{_bg_api()}")
        return
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(data)
            tmp_path = f.name
        try:
            path_str = tmp_path.replace("\\", "/")
            cq = f"[CQ:image,file=file:///{path_str}]"
            ctx.send_msg(
                user_id=event.user_id,
                group_id=event.group_id if event.is_group else None,
                message=cq,
            )
            _reply(event, f"签到背景图API：{_bg_api()}")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as e:
        _reply(event, f"背景图处理失败：{e}")


def handle_set_style(event, match):
    """设置群默认签到样式（管理员）"""
    gid = _gid(event)
    if not gid:
        _reply(event, "请在群聊中使用此命令设置群默认样式。")
        return
    # 权限检查（框架层已通过 require_admin 拦截，此处兜底）
    if not event.is_admin:
        _reply(event, "仅管理员可设置群默认签到样式。")
        return

    try:
        arg = (match.group(1) if (match and match.groups()) else "").strip()
        style = int(arg)
    except Exception:
        _reply(event, "用法：设置签到预设 [0-3]")
        return

    if style < 0 or style > 3:
        _reply(event, "样式编号应为 0-3")
        return

    try:
        _set_group_style(gid, style)
    except Exception as e:
        _reply(event, f"设置失败：{e}")
        return
    _reply(event, f"本群默认签到样式已设置为 {style}")


# ====================================================================
#  用户同步
# ====================================================================

def _sync_user(event):
    """同步用户到 fun_user 表（sid -> uid 映射）"""
    if _user_db is None:
        return
    uid = event.user_id
    nickname = _nickname(event)
    _user_db.register_or_update(str(uid), nickname)


# ====================================================================
#  违禁词检测（订阅 message 事件）
# ====================================================================

def on_message(event, match=None):
    """被动消息处理：违禁词检测（打码 + 撤回 + 禁言）

    match 对于事件订阅为 None。
    """
    if _ban_word_handler is None:
        return

    # 仅群聊启用违禁词检测
    if not event.is_group or not event.group_id:
        return

    # 管理员/群主/超管跳过检测
    try:
        role = ctx.get_user_role(event.group_id, event.user_id)
        if role in ("super", "owner", "admin"):
            return
    except Exception:
        pass

    # 同步用户
    try:
        _sync_user(event)
    except Exception:
        pass

    # 执行违禁词检测
    _ban_word_handler.on_ban_words(event)


# ====================================================================
#  LLM 工具注册
# ====================================================================

def _register_llm_tools():
    """通过 sys.modules 获取 llm_core 模块，注册本插件提供的 LLM 工具"""
    llm_core = sys.modules.get("plugin_llm_core")
    if llm_core is None:
        ctx.log("llm_core 未加载，跳过 LLM 工具注册", level="info")
        return

    try:
        llm_core.register_tool(
            plugin_name="fun_score",
            tool_name="sign_in",
            description="每日签到，获取经验和金币。可选样式参数 style（0-3），如果不指定则使用群默认样式。",
            parameters={
                "type": "object",
                "properties": {
                    "style": {
                        "type": "integer",
                        "description": "签到样式编号 0-3，可选，不指定则使用群默认样式",
                        "default": None,
                    }
                },
                "required": [],
            },
            handler=tool_sign_in,
        )
        llm_core.register_tool(
            plugin_name="fun_score",
            tool_name="view_score_rank",
            description="查看签到等级排名前10名，返回排行榜图片",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=tool_view_score_rank,
        )
        ctx.log("已注册 2 个 LLM 工具（fun_score）")
    except Exception as e:
        ctx.log(f"注册 LLM 工具失败: {e}", level="warning")


def _get_current_event():
    """
    获取当前对话事件上下文（由 llm_core 在工具调用前注入）。
    参考 payqr 插件的 _current_event 模式。
    """
    llm_core = sys.modules.get("plugin_llm_core")
    if llm_core is not None:
        getter = getattr(llm_core, "get_current_event", None)
        if not callable(getter):
            tools_mod = getattr(llm_core, "_tools_module", None)
            getter = getattr(tools_mod, "get_current_event", None) if tools_mod else None
        if callable(getter):
            ev = getter()
            if ev is not None:
                return ev
    return None


def _send_image_llm(event, image_bytes) -> bool:
    """LLM 工具中发送图片"""
    if not image_bytes:
        return False
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(image_bytes)
            tmp_path = f.name
        path_str = tmp_path.replace("\\", "/")
        cq = f"[CQ:image,file=file:///{path_str}]"
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message=cq,
        )
        return True
    except Exception as e:
        ctx.log(f"LLM工具发送图片失败: {e}", level="warning")
        return False
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _llm_sign_in(uid, gid, nickname, style=None):
    """执行签到逻辑（在 LLM 工具上下文下使用）"""
    if style is None:
        style = _get_group_style(gid)
    if style < 0 or style > 3:
        style = _get_group_style(gid)

    today = datetime.date.today()
    row = _ensure_user(uid, gid)
    if row is None:
        return "签到失败：无法读取用户数据，请稍后再试。"

    last = _parse_last_date(row.get("last_sign_date"))
    sign_count = int(row.get("sign_count") or 0)
    max_exp = _max_exp()
    base_coins = _base_coins()

    # 已签到
    if last is not None and last == today:
        exp = int(row.get("score") or 0)
        gold = int(row.get("gold") or 0)
        rank = get_rank(exp)
        sign_rank = _get_today_sign_rank(uid, gid)
        data = {
            "uid": uid, "nickname": nickname, "inc": 0, "gold": gold,
            "exp": exp, "rank": rank,
            "sign_rank_today": sign_rank["rank"],
            "total_sign_today": sign_rank["total"],
            "total_sign_days": sign_count,
        }
        return data

    # 未签到：执行签到
    old_exp = int(row.get("score") or 0)
    new_exp = min(old_exp + 1, max_exp)
    rank = get_rank(new_exp)
    inc = 1 + random.randint(1, base_coins) + rank * 5
    old_gold = int(row.get("gold") or 0)
    new_gold = old_gold + inc
    new_sign_count = sign_count + 1

    try:
        ctx.db_execute(
            "INSERT INTO fun_score (user_id, group_id, score, gold, level, sign_count, last_sign_date) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE score=VALUES(score), gold=VALUES(gold), "
            "level=VALUES(level), sign_count=VALUES(sign_count), last_sign_date=VALUES(last_sign_date)",
            (int(uid), int(gid), new_exp, new_gold, rank, new_sign_count, today),
        )
    except Exception as e:
        return f"签到失败：写入数据出错 - {e}"

    # 同步到 fun_user 表
    if _user_db is not None:
        try:
            _user_db.update_sign(str(uid), new_exp, new_gold, new_sign_count, today)
        except Exception:
            pass

    sign_rank = _get_today_sign_rank(uid, gid)
    data = {
        "uid": uid, "nickname": nickname, "inc": inc, "gold": new_gold,
        "exp": new_exp, "rank": rank,
        "sign_rank_today": sign_rank["rank"],
        "total_sign_today": sign_rank["total"],
        "total_sign_days": new_sign_count,
    }
    return data


def tool_sign_in(**kwargs):
    """LLM 工具：每日签到"""
    ev = _get_current_event()
    if ev is None:
        return "未获取到当前对话上下文，无法签到。"
    uid = ev.user_id
    gid = _gid(ev)
    nickname = _nickname(ev)
    style = kwargs.get("style")
    if style is None or not isinstance(style, int) or style < 0 or style > 3:
        style = _get_group_style(gid)

    result = _llm_sign_in(uid, gid, nickname, style)
    if isinstance(result, str):
        return result  # 错误消息

    data = result
    # 尝试发送图片（使用确定的 style）
    ok = _render_sign(ev, data, style)
    if ok:
        return "签到成功！图片已发送。"
    # 回退纯文本
    msg = "签到成功"
    if data["exp"] >= _max_exp():
        msg += "，你的经验已经达到上限"
    return (f"=== {msg} ===\n"
            f"{data['nickname']}\n"
            f"金币 + {data['inc']}  当前金币：{data['gold']}\n"
            f"经验 +1  当前经验：{data['exp']}/{_max_exp()}\n"
            f"等级：LV{data['rank']}\n"
            f"累计签到：{data['total_sign_days']} 天\n"
            f"今日第 {data['sign_rank_today']} 个签到（共 {data['total_sign_today']} 人）")


def tool_view_score_rank(**kwargs):
    """LLM 工具：查看签到等级排名"""
    ev = _get_current_event()
    if ev is None:
        return "未获取到当前对话上下文。"
    gid = _gid(ev)
    rank_list = _get_rank_list(gid, 10)
    if not rank_list:
        return "暂无积分排行数据，快来成为第一名吧！"

    ok = _render_rank(ev, rank_list)
    if ok:
        return "排行榜图片已发送。"
    # 回退纯文本
    scope = "本群" if gid else "全局"
    lines = [f"=== {scope}等级排行榜 TOP{len(rank_list)} ==="]
    medals = ["[1]", "[2]", "[3]"]
    for i, r in enumerate(rank_list):
        rank = i + 1
        prefix = medals[i] if i < 3 else f"[{rank}]"
        lines.append(f"{prefix} {r['nickname']}  经验:{r['score']}  LV{get_rank(r['score'])}")
    return "\n".join(lines)


# ====================================================================
#  卸载清理
# ====================================================================

def on_unload():
    """插件卸载：反注册 LLM 工具"""
    global _message_subscribed
    _message_subscribed = False
    # 反注册 LLM 工具
    llm_core = sys.modules.get("plugin_llm_core")
    if llm_core is not None:
        try:
            unregister = getattr(llm_core, "unregister_plugin_tools", None)
            if unregister:
                unregister("fun_score")
        except Exception as e:
            ctx.log(f"反注册 LLM 工具失败: {e}", level="warning")
    ctx.log("签到积分插件已卸载")

"""
qqadmin 群管插件 - 主入口
==========================
功能：禁言/全禁/踢人/拉黑/改昵称/头衔/管理员/精华/撤回/头像/群名/公告/群友信息/
      宵禁/刷屏/违禁词(本地+API)/进群管理/投票禁言/协管/配置管理/群信息卡片

权限分级（注意：群管权限较大，严格分级）：
  super > owner > admin > 协管 > member
  - 协管：可执行管理操作(禁言/踢/撤回等)，不可改配置/管理协管
  - admin：可执行管理操作+改配置，不可管理协管/上管/头衔
  - owner：可执行所有操作+管理协管+上管/下管/头衔
  - super：可执行一切操作（高于 owner）

数据库/用户/群信息全部走框架 ctx 接口（event.role / users / group_members）。
"""
import re
import os
import sys
import tempfile
import time

import guard
import store

# 将 guard 中的任务/兜底函数提升到 main 模块命名空间（scheduler/router 通过 getattr 查找）
from guard import curfew_tick, vote_tick, on_message_fallback

__plugin_meta__ = {
    "name": "qqadmin",
    "version": "1.1.0",
    "author": "ZGRIC",
    "desc": "QQ群管插件：禁言/踢人/全禁/精华/公告/宵禁/违禁词(本地+API)/进群管理/协管等",
    "priority": 90,
}

# 模块级 ctx（由框架注入）
ctx = None


# ===================== 工具函数 =====================


def _cfg(event):
    """获取本群配置"""
    return store.GroupConfigStore(ctx).get_group(event.group_id)


def _store(event):
    """获取配置存储实例"""
    return store.GroupConfigStore(ctx)


def _reply(event, message):
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=message,
    )


def _send_group(gid, message):
    ctx.api("send_group_msg", group_id=gid, message=message)


def _targets(event, text):
    """解析目标 QQ：优先 @，其次参数中数字"""
    ats = [u for u in (event.at_list or []) if str(u).isdigit()]
    if ats:
        return ats
    m = re.search(r"\d{5,}", text or "")
    return [int(m.group(0))] if m else []


def _get_nickname(gid, uid):
    try:
        info = ctx.get_member_info(gid, uid)
        if info:
            return info.get("card") or info.get("nickname") or str(uid)
    except Exception:
        pass
    return str(uid)


def _send_image(event, path):
    """发送本地图片文件并清理"""
    try:
        path_str = path.replace("\\", "/")
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message=f"[CQ:image,file=file:///{path_str}]",
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _try_send_card_image(event, title, lines, width=600, footer=None):
    """尝试用 image_renderer 官方版 _render_card_image 渲染信息卡片并发送。

    image_renderer 官方版只提供模块级渲染函数（_render_card_image 等），
    没有旧的 get_renderer 注册表 / HTTP 端口服务；这里直接调用官方接口，
    原生返回 PNG bytes，PIL 回退返回 Image，统一转 bytes 后写临时文件发送。
    成功返回 True，失败返回 False（调用方回退纯文本）。
    """
    mod = sys.modules.get("plugin_image_renderer")
    if mod is None or not hasattr(mod, "_render_card_image"):
        return False
    try:
        image = mod._render_card_image(
            title,
            "\n".join(lines),
            width=width,
            padding=28,
            options={
                "content_size": 18,
                "line_height": 28,
                "show_footer": True,
                "footer_text": footer or "ZGRIC OneBot",
                "bg_gradient": [(248, 250, 255, 255), (255, 255, 245, 255)],
                "radius": 16,
                "border_color": (224, 226, 240, 255),
                "border_width": 2,
                "accent_color": (99, 102, 241, 255),
            },
        )
        if image is None:
            return False
        # 原生返回 PNG bytes，PIL 回退返回 Image，统一转 bytes
        if isinstance(image, (bytes, bytearray)):
            image_bytes = bytes(image)
        else:
            buf = tempfile.SpooledTemporaryFile(mode="w+b")
            image.save(buf, format="PNG")
            buf.seek(0)
            image_bytes = buf.read()
            buf.close()
        tmp_path = os.path.join(
            tempfile.gettempdir(),
            f"qqadmin_{int(time.time()*1000)}_{abs(hash(title)) % 100000}.png",
        )
        with open(tmp_path, "wb") as f:
            f.write(image_bytes)
        _send_image(event, tmp_path)
        return True
    except Exception as e:
        ctx.logger.warning(f"{title} 图渲染失败: {e}")
        return False


# ===================== 权限检查 =====================


def _is_owner(event):
    """是否群主或超管（管理协管/上管/头衔等敏感操作）"""
    return event.role in ("super", "owner")


def _check_perm(event, need_owner=False):
    """统一权限检查，未通过则回复提示"""
    cfg = _cfg(event)
    if need_owner:
        if not _is_owner(event):
            _reply(event, "权限不足，需要群主或超级管理员权限")
            return False
    else:
        if not guard.can_manage(event, cfg):
            _reply(event, "权限不足，需要管理员、协管或以上权限")
            return False
    return True


# ===================== 群信息（图片渲染） =====================


def handle_group_info(event, match):
    """群信息 - 查看本群状态与配置（所有成员可用，图片渲染）"""
    gid = event.group_id
    if not gid:
        _reply(event, "请在群聊中使用")
        return

    cfg = _cfg(event)

    # 获取群信息
    group_name = str(gid)
    member_count = 0
    owner_list = []
    admin_list = []
    try:
        info = ctx.api("get_group_info", group_id=gid)
        if info and isinstance(info, dict):
            data = info.get("data") or info
            group_name = data.get("group_name", str(gid))
            member_count = data.get("member_count", 0)
    except Exception:
        pass

    # 获取管理员列表
    try:
        resp = ctx.api("get_group_member_list", group_id=gid)
        members = []
        if resp and isinstance(resp, dict):
            raw = resp.get("data") or resp
            if isinstance(raw, list):
                members = raw
        for m in members:
            role = m.get("role", "")
            uid = m.get("user_id", 0)
            card = m.get("card") or m.get("nickname") or str(uid)
            if role == "owner":
                owner_list.append(f"群主 {card}({uid})")
            elif role == "admin":
                admin_list.append(f"管理 {card}({uid})")
    except Exception:
        pass

    # 协管列表
    assistant_uids = store.get_assistants(cfg)
    assistant_names = []
    for uid in assistant_uids:
        nick = _get_nickname(gid, int(uid))
        assistant_names.append(f"协管 {nick}({uid})")

    # 构建卡片行
    lines = [
        f"群名: {group_name}",
        f"群号: {gid}",
        f"成员数: {member_count}",
        "---",
        "## 管理人员",
    ]
    for o in owner_list or ["  群主 (无)"]:
        lines.append(f"  {o}")
    for a in admin_list or ["  管理员 (无)"]:
        lines.append(f"  {a}")
    for a in assistant_names or ["  协管 (无)"]:
        lines.append(f"  {a}")

    lines.append("---")
    lines.append("## 功能开关")

    # 从配置读取各功能状态
    def _yn(v): return "开 ✅" if v else "关 ❌"
    def _yn2(v): return "开 ✅" if v else "关"

    features = [
        ("违禁词检测", _yn(cfg.get("ban_word_enabled")) + f" 模式:{cfg.get('ban_word_mode','local')}"),
        ("内置禁词", _yn(cfg.get("builtin_ban_words"))),
        ("刷屏检测", _yn(cfg.get("spam_enabled"))),
        ("宵禁", _yn(cfg.get("curfew_enabled")) + (f" {cfg.get('curfew_start','?')}-{cfg.get('curfew_end','?')}" if cfg.get("curfew_enabled") else "")),
        ("进群欢迎", _yn(cfg.get("join_welcome"))),
        ("进群审核", _yn(cfg.get("join_review"))),
        ("退群通知", _yn(cfg.get("leave_notify"))),
        ("退群拉黑", _yn(cfg.get("leave_blacklist"))),
    ]
    for label, status in features:
        lines.append(f"  {label}: {status}")

    # 尝试图片渲染（image_renderer 官方版 _render_card_image），失败回退纯文本
    if not _try_send_card_image(
        event, "群信息", lines, 600,
        footer=f"ZGRIC OneBot · {time.strftime('%Y-%m-%d %H:%M')}",
    ):
        _reply(event, "\n".join(lines))


# ===================== 禁言 / 解禁 =====================


def handle_ban(event, match):
    """禁言 <秒数> [@用户|QQ]"""
    if not _check_perm(event): return
    gid = event.group_id
    text = match.group(1) if match else ""
    parts = text.strip().split(None, 1) if text else ["", ""]
    try:
        duration = int(parts[0])
    except (ValueError, IndexError):
        _reply(event, "用法: 禁言 <秒数> [@用户]")
        return
    targets = _targets(event, parts[1] if len(parts) > 1 else "")
    if not targets:
        _reply(event, "请指定要禁言的用户（@或QQ号）")
        return
    for uid in targets:
        try:
            ctx.ban(gid, uid, duration)
        except Exception as e:
            ctx.logger.warning(f"[qqadmin] 禁言失败 {uid}: {e}")
    _reply(event, f"已禁言 {len(targets)} 人 {duration} 秒")


def handle_ban_me(event, match):
    """禁我 <秒数>"""
    gid = event.group_id
    if not gid:
        _reply(event, "请在群聊中使用")
        return
    text = match.group(1) if match else ""
    try:
        duration = int(text.strip())
    except ValueError:
        _reply(event, "用法: 禁我 <秒数>")
        return
    try:
        ctx.ban(gid, event.user_id, duration)
    except Exception as e:
        _reply(event, f"禁言失败: {e}")
        return
    _reply(event, f"已禁言自己 {duration} 秒")


def handle_unban(event, match):
    """解禁 [@用户|QQ]"""
    if not _check_perm(event): return
    gid = event.group_id
    targets = _targets(event, match.group(1) if match else "")
    if not targets:
        _reply(event, "请指定要解禁的用户（@或QQ号）")
        return
    for uid in targets:
        try:
            ctx.ban(gid, uid, 0)
        except Exception as e:
            ctx.logger.warning(f"[qqadmin] 解禁失败 {uid}: {e}")
    _reply(event, f"已解禁 {len(targets)} 人")


def handle_mute_all(event, match):
    """开启全禁 / 关闭全禁"""
    if not _check_perm(event): return
    gid = event.group_id
    text = event.message or ""
    enable = "开启" in text or "开" in text
    try:
        ctx.mute_all(gid, enable)
    except Exception as e:
        _reply(event, f"操作失败: {e}")
        return
    _reply(event, "已开启全员禁言" if enable else "已关闭全员禁言")


# ===================== 改名 / 头衔 =====================


def handle_rename(event, match):
    """改名 <新昵称> [@用户]"""
    if not _check_perm(event): return
    gid = event.group_id
    text = (match.group(1) or "").strip()
    if not text:
        _reply(event, "用法: 改名 <新昵称> [@用户]")
        return
    targets = _targets(event, text)
    if targets:
        uid = targets[0]
        # 去掉命令中的 @ 部分取昵称
        nick = re.sub(r"\d{5,}", "", text).strip()
    else:
        uid = event.user_id
        nick = text
    if not nick:
        _reply(event, "昵称不能为空")
        return
    try:
        ctx.set_card(gid, uid, nick)
    except Exception as e:
        _reply(event, f"改名失败: {e}")
        return
    _reply(event, f"已修改 {_get_nickname(gid, uid)} 的名片为: {nick}")


def handle_rename_me(event, match):
    """改我 <新昵称>"""
    gid = event.group_id
    nick = (match.group(1) or "").strip()
    if not nick:
        _reply(event, "用法: 改我 <新昵称>")
        return
    try:
        ctx.set_card(gid, event.user_id, nick)
    except Exception as e:
        _reply(event, f"改名失败: {e}")
        return
    _reply(event, f"已修改你的名片为: {nick}")


def handle_title(event, match):
    """头衔 <头衔> [@用户]（需群主/超管）"""
    if not _check_perm(event, need_owner=True): return
    gid = event.group_id
    text = (match.group(1) or "").strip()
    if not text:
        _reply(event, "用法: 头衔 <头衔> [@用户]")
        return
    targets = _targets(event, text)
    uid = targets[0] if targets else event.user_id
    title = re.sub(r"\d{5,}", "", text).strip()
    try:
        ctx.api("set_group_special_title", group_id=gid, user_id=uid, special_title=title)
    except Exception as e:
        _reply(event, f"设置头衔失败: {e}")
        return
    _reply(event, f"已设置 {_get_nickname(gid, uid)} 的头衔为: {title}")


# ===================== 踢人 / 拉黑 =====================


def handle_kick(event, match):
    """踢了 [@用户|QQ]"""
    if not _check_perm(event): return
    gid = event.group_id
    targets = _targets(event, match.group(1) if match else "")
    if not targets:
        _reply(event, "请指定要踢出的用户（@或QQ号）")
        return
    for uid in targets:
        try:
            ctx.kick(gid, uid)
        except Exception as e:
            ctx.logger.warning(f"[qqadmin] 踢人失败 {uid}: {e}")
    _reply(event, f"已踢出 {len(targets)} 人")


def handle_blacklist(event, match):
    """拉黑 [@用户|QQ]"""
    if not _check_perm(event): return
    gid = event.group_id
    targets = _targets(event, match.group(1) if match else "")
    if not targets:
        _reply(event, "请指定要拉黑的用户（@或QQ号）")
        return
    for uid in targets:
        try:
            ctx.kick(gid, uid, reject_add_request=True)
        except Exception as e:
            ctx.logger.warning(f"[qqadmin] 拉黑失败 {uid}: {e}")
        # 同时加入进群黑名单
        cfg = _cfg(event)
        bl = (cfg.get("join_blacklist") or "").split()
        if str(uid) not in bl:
            bl.append(str(uid))
            _store(event).set_group(gid, "join_blacklist", " ".join(bl))
    _reply(event, f"已拉黑 {len(targets)} 人")


# ===================== 管理员设置 =====================


def handle_set_admin(event, match):
    """上管 @用户 / 下管 @用户（需群主/超管）"""
    if not _check_perm(event, need_owner=True): return
    gid = event.group_id
    targets = _targets(event, match.group(1) if match else "")
    if not targets:
        _reply(event, "请指定用户（@或QQ号）")
        return
    enable = "上管" in (event.message or "")
    for uid in targets:
        try:
            ctx.api("set_group_admin", group_id=gid, user_id=uid, enable=enable)
        except Exception as e:
            ctx.logger.warning(f"[qqadmin] 设置管理员失败 {uid}: {e}")
    _reply(event, f"已{'设置' if enable else '取消'}{len(targets)} 人为管理员")


# ===================== 撤回 =====================


def handle_recall(event, match):
    """撤回 (引用消息) / 撤回 @用户 数量"""
    if not _check_perm(event): return
    gid = event.group_id
    reply_id = event.reply_id
    if reply_id:
        try:
            ctx.api("delete_msg", message_id=reply_id)
            _reply(event, "已撤回引用消息")
            return
        except Exception as e:
            _reply(event, f"撤回失败: {e}")
            return
    # @用户 数量
    targets = _targets(event, match.group(1) if match else "")
    if targets:
        uid = targets[0]
        text = match.group(1) if match else ""
        m = re.search(r"(\d+)", text)
        count = int(m.group(1)) if m else 10
        mids = guard.get_recent_msg_ids(gid, uid, count)
        if not mids:
            _reply(event, f"没有找到 {uid} 的最近消息记录")
            return
        success = 0
        for mid in mids:
            try:
                ctx.api("delete_msg", message_id=mid)
                success += 1
            except Exception:
                pass
        _reply(event, f"已撤回 {uid} 的 {success} 条消息")
    else:
        _reply(event, "用法: 撤回 (引用消息) 或 撤回 @用户 [数量]")


# ===================== 精华消息 =====================


def handle_essence(event, match):
    """设精 (引用消息) / 移精 (引用消息)"""
    if not _check_perm(event): return
    reply_id = event.reply_id
    if not reply_id:
        _reply(event, "请引用要操作的消息")
        return
    is_set = "设精" in (event.message or "")
    action = "set_essence_msg" if is_set else "delete_essence_msg"
    try:
        ctx.api(action, message_id=reply_id)
        _reply(event, "已设置精华消息" if is_set else "已移除精华消息")
    except Exception as e:
        _reply(event, f"操作失败（NapCat 扩展API）: {e}")


# ===================== 群头像 / 群名 / 公告 =====================


def handle_set_portrait(event, match):
    """设置群头像 (引用图片)"""
    if not _check_perm(event): return
    gid = event.group_id
    img_url = store.get_image_url(event)
    if not img_url:
        _reply(event, "请引用包含图片的消息")
        return
    try:
        ctx.api("set_group_portrait", group_id=gid, file=img_url)
        _reply(event, "已尝试修改群头像（NapCat 扩展API）")
    except Exception as e:
        _reply(event, f"修改群头像失败: {e}")


def handle_set_group_name(event, match):
    """设置群名 <新群名>"""
    if not _check_perm(event): return
    gid = event.group_id
    name = (match.group(1) or "").strip()
    if not name:
        _reply(event, "用法: 设置群名 <新群名>")
        return
    try:
        ctx.api("set_group_name", group_id=gid, group_name=name)
        _reply(event, f"群名已修改为: {name}")
    except Exception as e:
        _reply(event, f"修改群名失败: {e}")


def handle_notice(event, match):
    """发布群公告 <内容>"""
    if not _check_perm(event): return
    gid = event.group_id
    content = (match.group(1) or "").strip()
    if not content:
        _reply(event, "用法: 发布群公告 <内容>")
        return
    params = {"group_id": gid, "content": content}
    # 如果有引用图片，一并发送
    img_url = store.get_image_url(event)
    if img_url:
        params["image"] = img_url
    try:
        ctx.api("send_group_notice", **params)
        _reply(event, "群公告已发布（NapCat 扩展API）")
    except Exception as e:
        _reply(event, f"发布公告失败: {e}")


# ===================== 群友信息 =====================


def handle_member_info(event, match):
    """群友信息 - 查看群成员活跃情况"""
    if not _check_perm(event): return
    gid = event.group_id
    try:
        members = ctx.get_member_list(gid) or []
    except Exception as e:
        _reply(event, f"获取成员列表失败: {e}")
        return
    total = len(members)
    owner_count = sum(1 for m in members if m.get("role") == "owner")
    admin_count = sum(1 for m in members if m.get("role") == "admin")
    member_count = total - owner_count - admin_count
    _reply(event, f"本群成员概况:\n总人数: {total}\n群主: {owner_count}\n管理员: {admin_count}\n成员: {member_count}")


def handle_clean_member(event, match):
    """清理群友 <天数> <等级>"""
    if not _check_perm(event): return
    gid = event.group_id
    text = (match.group(1) or "").strip()
    parts = text.split()
    try:
        days = int(parts[0]) if parts else 30
        min_level = int(parts[1]) if len(parts) > 1 else 1
    except ValueError:
        _reply(event, "用法: 清理群友 <未发言天数> <群等级>")
        return
    try:
        members = ctx.get_member_list(gid) or []
    except Exception as e:
        _reply(event, f"获取成员列表失败: {e}")
        return
    # 仅处理有 last_sent_time 的成员，且角色为 member，等级低于门槛
    to_kick = []
    for m in members:
        if m.get("role") != "member":
            continue
        uid = m.get("user_id", 0)
        lst = m.get("last_sent_time", 0)
        if not lst:
            continue
        now = time.time()
        if now - lst > days * 86400:
            to_kick.append(uid)
    if not to_kick:
        _reply(event, f"没有找到符合条件的成员（{days}天未发言）")
        return
    _reply(event, f"找到 {len(to_kick)} 个符合条件的成员，请逐个使用「踢了」命令处理")


# ===================== 违禁词 / 刷屏 配置 =====================


def _toggle_bool(event, key, label):
    if not _check_perm(event): return
    gid = event.group_id
    text = (event.message or "").strip()
    # 解析开/关
    if "开" in text or "on" in text.lower():
        val = True
    elif "关" in text or "off" in text.lower():
        val = False
    else:
        _reply(event, f"用法: {label} 开/关")
        return
    _store(event).set_group(gid, key, val)
    _reply(event, f"{label} 已{'开启' if val else '关闭'}")


def handle_set_ban_word(event, match):
    """设置禁词 <词1 词2...>"""
    if not _check_perm(event): return
    gid = event.group_id
    words = (match.group(1) or "").strip()
    _store(event).set_group(gid, "ban_word_list", words.replace(" ", ","))
    _reply(event, "违禁词已更新")


def handle_ban_word_mode(event, match):
    """违禁词模式 本地/api/双检"""
    if not _check_perm(event): return
    gid = event.group_id
    mode = (match.group(1) or "").strip().lower()
    if mode not in ("local", "api", "双检", "both"):
        _reply(event, "用法: 违禁词模式 local/api/双检")
        return
    key = "both" if mode in ("双检", "both") else mode
    _store(event).set_group(gid, "ban_word_mode", key)
    _reply(event, f"违禁词模式已切换为: {key}")


# ===================== 宵禁 =====================


def handle_curfew(event, match):
    """开启宵禁 HH:MM HH:MM / 关闭宵禁"""
    if not _check_perm(event): return
    gid = event.group_id
    text = (match.group(1) or "").strip()
    if "关闭" in (event.message or ""):
        _store(event).set_group(gid, "curfew_enabled", False)
        _reply(event, "宵禁已关闭")
        return
    m = re.match(r"(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})", text)
    if not m:
        _reply(event, "用法: 开启宵禁 HH:MM HH:MM")
        return
    _store(event).set_group(gid, "curfew_start", m.group(1))
    _store(event).set_group(gid, "curfew_end", m.group(2))
    _store(event).set_group(gid, "curfew_enabled", True)
    _reply(event, f"宵禁已开启: {m.group(1)} - {m.group(2)}")


# ===================== 进群配置 =====================


def handle_join_review(event, match):
    """进群审核 开/关"""
    _toggle_bool(event, "join_review", "进群审核")


def handle_join_welcome(event, match):
    """进群欢迎 开/关"""
    _toggle_bool(event, "join_welcome", "进群欢迎")


def handle_leave_notify(event, match):
    """退群通知 开/关"""
    _toggle_bool(event, "leave_notify", "退群通知")


def handle_leave_blacklist(event, match):
    """退群拉黑 开/关"""
    _toggle_bool(event, "leave_blacklist", "退群拉黑")


def handle_join_blacklist_add(event, match):
    """进群黑名单 +QQ / -QQ"""
    if not _check_perm(event): return
    gid = event.group_id
    text = (match.group(1) or "").strip()
    m = re.match(r"([+-])(\d+)", text)
    if not m:
        _reply(event, "用法: 进群黑名单 +QQ 或 -QQ")
        return
    op, qq = m.group(1), m.group(2)
    cfg = _cfg(event)
    bl = (cfg.get("join_blacklist") or "").split()
    if op == "+":
        if qq not in bl:
            bl.append(qq)
            _store(event).set_group(gid, "join_blacklist", " ".join(bl))
        _reply(event, f"已添加 {qq} 到进群黑名单")
    else:
        bl = [u for u in bl if u != qq]
        _store(event).set_group(gid, "join_blacklist", " ".join(bl))
        _reply(event, f"已从进群黑名单移除 {qq}")


def handle_approve(event, match):
    """批准 [QQ]"""
    if not _check_perm(event): return
    extra = (match.group(1) or "").strip()
    guard.handle_review(event, True, extra)


def handle_reject(event, match):
    """驳回 [QQ]"""
    if not _check_perm(event): return
    extra = (match.group(1) or "").strip()
    guard.handle_review(event, False, extra)


# ===================== 投票禁言 =====================


def handle_vote_ban(event, match):
    """投票禁言 <秒数> [@用户]"""
    text = (match.group(1) or "").strip()
    m = re.search(r"(\d+)", text)
    if not m:
        _reply(event, "用法: 投票禁言 <秒数> [@用户]")
        return
    ban_time = int(m.group(1))
    targets = _targets(event, text)
    if not targets:
        _reply(event, "请指定要禁言的目标（@或QQ号）")
        return
    guard.start_vote(event, ban_time, targets)


def handle_vote_agree(event, match):
    """赞同禁言"""
    guard.vote(event, True)


def handle_vote_disagree(event, match):
    """反对禁言"""
    guard.vote(event, False)


# ===================== 协管管理 =====================


def handle_add_assistant(event, match):
    """添加协管 @用户（仅群主/超管）"""
    if not _check_perm(event, need_owner=True): return
    gid = event.group_id
    targets = _targets(event, match.group(1) if match else "")
    if not targets:
        _reply(event, "请指定用户（@要添加的成员）")
        return
    uid = targets[0]
    ok = store.add_assistant(_store(event), gid, uid)
    _reply(event, f"已添加 {_get_nickname(gid, uid)} 为协管" if ok else f"{_get_nickname(gid, uid)} 已经是协管了")


def handle_remove_assistant(event, match):
    """移除协管 @用户（仅群主/超管）"""
    if not _check_perm(event, need_owner=True): return
    gid = event.group_id
    targets = _targets(event, match.group(1) if match else "")
    if not targets:
        _reply(event, "请指定用户（@要移除的成员）")
        return
    uid = targets[0]
    existed = store.remove_assistant(_store(event), gid, uid)
    _reply(event, f"已移除 {_get_nickname(gid, uid)} 的协管身份" if existed else f"{_get_nickname(gid, uid)} 不是协管")


def handle_list_assistants(event, match):
    """协管列表"""
    gid = event.group_id
    if not gid:
        _reply(event, "请在群聊中使用")
        return
    uids = store.get_assistants(_cfg(event))
    if not uids:
        _reply(event, "本群暂无协管")
        return
    lines = ["本群协管列表:"]
    for uid in uids:
        nick = _get_nickname(gid, int(uid))
        lines.append(f"  {nick} ({uid})")
    _reply(event, "\n".join(lines))


# ===================== 配置管理 =====================


def handle_config(event, match):
    """群管配置 [文本]"""
    if not _check_perm(event): return
    gid = event.group_id
    text = (match.group(1) or "").strip()
    if text:
        _store(event).import_cn_lines(gid, text)
        _reply(event, "配置已更新")
    else:
        lines = _store(event).get_cn_lines(gid)
        _reply(event, f"本群群管配置:\n{lines}")


def handle_config_reset(event, match):
    """群管重置 [群号|all]（仅群主/超管）"""
    if not _check_perm(event, need_owner=True): return
    text = (match.group(1) or "").strip()
    target = text if text else "all" if "all" in (event.message or "") else ""
    _store(event).reset_group(target if target else str(event.group_id))
    _reply(event, f"已重置 {target or '本群'} 群管配置")


# ===================== 帮助 =====================


def handle_help(event, match):
    """/群管帮助"""
    lines = [
        "【群管命令集】",
        "---",
        "## 管理操作（管理员/协管可用）",
        "  禁言 <秒数> @用户",
        "  禁我 <秒数>",
        "  解禁 @用户",
        "  开启全禁 / 关闭全禁",
        "  改名 <昵称> @用户 / 改我 <昵称>",
        "  踢了 @用户",
        "  拉黑 @用户",
        "  撤回 (引用消息) / 撤回 @用户 数量",
        "  设精 (引用) / 移精 (引用)",
        "  设置群头像 (引用图片)",
        "  设置群名 <新群名>",
        "  发布群公告 <内容>",
        "  群友信息 / 清理群友 <天数> <等级>",
        "  批准 <QQ号> / 驳回 <QQ号>",
        "---",
        "## 配置管理（管理员可用）",
        "  群管配置 (查看/修改)",
        "  违禁词 开/关",
        "  违禁词模式 local/api/双检",
        "  设置禁词 <词...>",
        "  内置禁词 开/关",
        "  刷屏禁言 <秒数>",
        "  开启宵禁 HH:MM HH:MM / 关闭宵禁",
        "  进群审核 开/关 / 进群欢迎 开/关",
        "  进群黑名单 +QQ / -QQ",
        "  退群通知 开/关 / 退群拉黑 开/关",
        "---",
        "## 协管管理（仅群主/超管）",
        "  添加协管 @用户 / 移除协管 @用户",
        "  上管 @用户 / 下管 @用户",
        "  头衔 <头衔> @用户",
        "  群管重置 [群号|all]",
        "---",
        "## 其他",
        "  群信息 (查看本群状态)",
        "  协管列表",
        "  投票禁言 <秒数> @用户",
        "  赞同禁言 / 反对禁言",
        "  /群管帮助",
    ]
    # 尝试图片渲染（image_renderer 官方版 _render_card_image），失败回退纯文本
    if not _try_send_card_image(event, "群管帮助", lines, 560):
        _reply(event, "\n".join(lines))


# ===================== 注册入口 =====================


def register(ctx_obj):
    """注册所有命令、事件、定时任务"""
    global ctx
    ctx = ctx_obj
    guard.ctx = ctx  # 注入 guard 模块的 ctx

    # ── 事件订阅 ──
    ctx.on("notice.group_increase", guard.on_group_increase)
    ctx.on("notice.group_decrease", guard.on_group_decrease)
    ctx.on("request.group", guard.on_group_request)

    # ── 定时任务 ──
    ctx.task("*/1 * * * *", curfew_tick, description="群管宵禁检查")
    ctx.task("*/1 * * * *", vote_tick, description="群管投票统计")

    # ── 兜底命令（违禁词/刷屏检测，priority=100 最后执行）──
    ctx.command("^", on_message_fallback, priority=100,
                description="群管消息兜底检测")

    # ── 管理操作（admin+ / 协管可用）──
    ctx.command(r"^禁言\s+(\d+)\s*(.*)$", handle_ban, priority=50, description="禁言用户")
    ctx.command(r"^禁我\s+(\d+)\s*$", handle_ban_me, priority=50, description="禁言自己")
    ctx.command(r"^解禁\s*(.*)$", handle_unban, priority=50, description="解除禁言")
    ctx.command(r"^开启全禁\s*$", handle_mute_all, priority=50, description="开启全员禁言")
    ctx.command(r"^关闭全禁\s*$", handle_mute_all, priority=50, description="关闭全员禁言")
    ctx.command(r"^改名\s+(.+)$", handle_rename, priority=50, description="修改群名片")
    ctx.command(r"^改我\s+(.+)$", handle_rename_me, priority=50, description="修改自己的群名片")
    ctx.command(r"^踢了\s*(.*)$", handle_kick, priority=50, description="踢出群成员")
    ctx.command(r"^拉黑\s*(.*)$", handle_blacklist, priority=50, description="拉黑群成员")
    ctx.command(r"^撤回\s*(.*)$", handle_recall, priority=50, description="撤回消息")
    ctx.command(r"^设精\s*(.*)$", handle_essence, priority=50, description="设置精华消息")
    ctx.command(r"^移精\s*(.*)$", handle_essence, priority=50, description="移除精华消息")
    ctx.command(r"^设置群头像\s*(.*)$", handle_set_portrait, priority=50, description="修改群头像")
    ctx.command(r"^设置群名\s+(.+)$", handle_set_group_name, priority=50, description="修改群名称")
    ctx.command(r"^发布群公告\s+(.+)$", handle_notice, priority=50, description="发布群公告")
    ctx.command(r"^群友信息\s*$", handle_member_info, priority=50, description="群成员概况")
    ctx.command(r"^清理群友\s+(.+)$", handle_clean_member, priority=50, description="清理不活跃成员")
    ctx.command(r"^批准\s+(\d+)\s*$", handle_approve, priority=50, description="批准进群申请：批准 <QQ号>")
    ctx.command(r"^驳回\s+(\d+)\s*$", handle_reject, priority=50, description="驳回进群申请：驳回 <QQ号>")

    # ── 群主/超管专属操作 ──
    ctx.command(r"^头衔\s+(.+)$", handle_title, priority=50, description="设置群头衔")
    ctx.command(r"^上管\s*(.*)$", handle_set_admin, priority=50, description="设置管理员")
    ctx.command(r"^下管\s*(.*)$", handle_set_admin, priority=50, description="取消管理员")
    ctx.command(r"^群管重置\s*(.*)$", handle_config_reset, priority=50, description="重置群管配置")

    # ── 配置管理（admin+）──
    ctx.command(r"^群管配置\s*(.*)$", handle_config, priority=50, description="查看/修改群管配置")
    ctx.command(r"^违禁词\s+开\s*$", handle_ban_word_toggle_on, priority=50, description="开启违禁词检测")
    ctx.command(r"^违禁词\s+关\s*$", handle_ban_word_toggle_off, priority=50, description="关闭违禁词检测")
    ctx.command(r"^违禁词模式\s+(.+)$", handle_ban_word_mode, priority=50, description="切换违禁词模式")
    ctx.command(r"^设置禁词\s+(.+)$", handle_set_ban_word, priority=50, description="设置自定义违禁词")
    ctx.command(r"^内置禁词\s+开\s*$", handle_builtin_ban_word_on, priority=50, description="开启内置禁词")
    ctx.command(r"^内置禁词\s+关\s*$", handle_builtin_ban_word_off, priority=50, description="关闭内置禁词")
    ctx.command(r"^刷屏禁言\s+(\d+)\s*$", handle_spam_ban, priority=50, description="设置刷屏禁言时长")
    ctx.command(r"^开启宵禁\s+(.+)$", handle_curfew, priority=50, description="开启宵禁")
    ctx.command(r"^关闭宵禁\s*$", handle_curfew, priority=50, description="关闭宵禁")
    ctx.command(r"^进群审核\s+开\s*$", handle_join_review_on, priority=50, description="开启进群审核")
    ctx.command(r"^进群审核\s+关\s*$", handle_join_review_off, priority=50, description="关闭进群审核")
    ctx.command(r"^进群欢迎\s+开\s*$", handle_join_welcome_on, priority=50, description="开启进群欢迎")
    ctx.command(r"^进群欢迎\s+关\s*$", handle_join_welcome_off, priority=50, description="关闭进群欢迎")
    ctx.command(r"^退群通知\s+开\s*$", handle_leave_notify_on, priority=50, description="开启退群通知")
    ctx.command(r"^退群通知\s+关\s*$", handle_leave_notify_off, priority=50, description="关闭退群通知")
    ctx.command(r"^退群拉黑\s+开\s*$", handle_leave_blacklist_on, priority=50, description="开启退群拉黑")
    ctx.command(r"^退群拉黑\s+关\s*$", handle_leave_blacklist_off, priority=50, description="关闭退群拉黑")
    ctx.command(r"^进群黑名单\s+([+-]\d+)\s*$", handle_join_blacklist_add, priority=50, description="管理进群黑名单")

    # ── 协管管理（仅群主/超管）──
    ctx.command(r"^添加协管\s*(.*)$", handle_add_assistant, priority=50, description="添加协管")
    ctx.command(r"^移除协管\s*(.*)$", handle_remove_assistant, priority=50, description="移除协管")
    ctx.command(r"^协管列表\s*$", handle_list_assistants, priority=50, description="查看协管列表")

    # ── 投票禁言（所有人）──
    ctx.command(r"^投票禁言\s+(.+)$", handle_vote_ban, priority=50, description="发起投票禁言")
    ctx.command(r"^赞同禁言\s*$", handle_vote_agree, priority=50, description="赞同禁言投票")
    ctx.command(r"^反对禁言\s*$", handle_vote_disagree, priority=50, description="反对禁言投票")

    # ── 群信息（所有人）──
    ctx.command(r"^群信息\s*$", handle_group_info, priority=50, description="查看本群状态信息")

    # ── 帮助（所有人）──
    ctx.command(r"^/群管帮助\s*$", handle_help, priority=50, description="群管命令帮助")

    ctx.logger.info("群管插件注册完成: 命令+事件+定时任务")


# ===================== 开/关 便捷 handler =====================


def handle_ban_word_toggle_on(event, match): _toggle_bool(event, "ban_word_enabled", "违禁词检测")
def handle_ban_word_toggle_off(event, match): _toggle_bool(event, "ban_word_enabled", "违禁词检测")
def handle_builtin_ban_word_on(event, match): _toggle_bool(event, "builtin_ban_words", "内置禁词")
def handle_builtin_ban_word_off(event, match): _toggle_bool(event, "builtin_ban_words", "内置禁词")
def handle_spam_ban(event, match):
    if not _check_perm(event): return
    gid = event.group_id
    sec = int(match.group(1))
    _store(event).set_group(gid, "spam_ban_time", sec)
    _store(event).set_group(gid, "spam_enabled", sec > 0)
    _reply(event, f"刷屏禁言已设置为 {sec} 秒{'（已关闭）' if sec == 0 else ''}")
def handle_join_review_on(event, match): _toggle_bool(event, "join_review", "进群审核")
def handle_join_review_off(event, match): _toggle_bool(event, "join_review", "进群审核")
def handle_join_welcome_on(event, match): _toggle_bool(event, "join_welcome", "进群欢迎")
def handle_join_welcome_off(event, match): _toggle_bool(event, "join_welcome", "进群欢迎")
def handle_leave_notify_on(event, match): _toggle_bool(event, "leave_notify", "退群通知")
def handle_leave_notify_off(event, match): _toggle_bool(event, "leave_notify", "退群通知")
def handle_leave_blacklist_on(event, match): _toggle_bool(event, "leave_blacklist", "退群拉黑")
def handle_leave_blacklist_off(event, match): _toggle_bool(event, "leave_blacklist", "退群拉黑")


def on_unload():
    """插件卸载时清理 guard 事件订阅"""
    guard.ctx = None
    ctx.logger.info("群管插件已卸载")
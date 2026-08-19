# -*- coding: utf-8 -*-
"""
签到核心绘制逻辑 + 4种预设样式（Canvas 版，无 PIL）
====================================================

从 astrbot_plugin_fun_score 迁移，绘图层全部改用
image_renderer 的原生 Canvas（Rust）与 image_* 图像处理，
彻底移除 PIL 绘制依赖。

- ScoreCore 保持纯绘制+下载工具类：下载背景/头像（bytes）+ 绘制 + 返回 PNG bytes
- 4 种样式 draw_score_15/16/17/17b2：Canvas 重绘（布局保持原版比例）
- 排行榜 render_rank_image：走 image_renderer.render_list_image（Rust 榜单）

通过 sys.modules["plugin_image_renderer"] 获取渲染后端
（image_renderer priority=200 最先加载，运行时必然就绪）。
"""

from __future__ import annotations

import os
import sys
import logging
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger("fun_score")

# ============================== 等级表 ==============================

# 11档等级（LV0-LV10），经验上限1200
rankArray = [0, 10, 20, 50, 100, 200, 350, 550, 750, 1000, 1200]

# 字体路径（从 core/score.py 回到插件根目录 resource/font/）
_FONT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resource", "font"
)
BOLD_FONT_PATH = os.path.join(_FONT_DIR, "HarmonyOS_Sans_SC_Bold.ttf")
REGULAR_FONT_PATH = os.path.join(_FONT_DIR, "HarmonyOS_Sans_SC_Regular.ttf")


def _mod():
    """获取 image_renderer 插件模块（懒获取）"""
    return sys.modules.get("plugin_image_renderer")


def get_rank(count: int) -> int:
    """根据经验值返回等级（0-10），复刻 ZeroBot getrank 逻辑"""
    for k, v in enumerate(rankArray):
        if count == v:
            return k
        elif count < v:
            return k - 1
    return len(rankArray) - 1


def get_hour_word(t: datetime) -> str:
    """根据小时返回时段问候语"""
    h = t.hour
    if 6 <= h < 12:
        return "早上好"
    elif 12 <= h < 14:
        return "中午好"
    elif 14 <= h < 19:
        return "下午好"
    elif 19 <= h < 24:
        return "晚上好"
    elif 0 <= h < 6:
        return "凌晨好"
    return ""


def _download_image(url: str, timeout: int = 10) -> Optional[bytes]:
    """下载图片，返回 bytes，失败返回 None（requests 同步）"""
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.debug("下载图片失败 %s: %s", url, e)
        return None


def _font_path() -> str:
    """返回 Canvas 用字体路径（粗体优先，缺失则回退常规）"""
    if os.path.isfile(BOLD_FONT_PATH):
        return BOLD_FONT_PATH
    return REGULAR_FONT_PATH


# ============================== 签到数据结构 ==============================

class ScData:
    """签到绘制数据（bg_image/avatar_image 为 PNG bytes）"""

    def __init__(
        self,
        uid: int,
        nickname: str,
        inc: int,
        score: int,
        level: int,
        rank: int,
        bg_image: Optional[bytes] = None,
        avatar_image: Optional[bytes] = None,
        total_sign_today: int = 0,
        sign_rank_today: int = 0,
        total_sign_days: int = 0,
    ):
        self.uid = uid
        self.nickname = nickname
        self.inc = inc            # 本次增加金币
        self.score = score        # 当前金币余额
        self.level = level        # 当前经验值
        self.rank = rank          # 当前等级
        self.bg_image = bg_image
        self.avatar_image = avatar_image
        self.total_sign_today = total_sign_today  # 今日总签到人数
        self.sign_rank_today = sign_rank_today    # 今日签到排名
        self.total_sign_days = total_sign_days    # 累计签到天数


# ============================== 绘制工具类 ==============================

class ScoreCore:
    """签到绘制+下载工具类（同步，无数据库操作）"""

    _STYLES = None

    def __init__(self, config: dict):
        self.bg_api = config.get("bg_api", "https://furry.axzt.top/")
        self.max_exp = int(config.get("max_exp", 1200))
        self.base_coins = int(config.get("base_coins", 10))

    @classmethod
    def _get_styles(cls):
        if cls._STYLES is None:
            cls._STYLES = [draw_score_15, draw_score_16, draw_score_17, draw_score_17b2]
        return cls._STYLES

    # ---------------------- 下载 ----------------------

    def _download_bg(self) -> Optional[bytes]:
        """下载签到背景图（返回 PNG bytes）"""
        return _download_image(self.bg_api)

    def _download_avatar(self, uid: int) -> Optional[bytes]:
        """下载用户头像（返回 PNG bytes）"""
        url = f"https://q4.qlogo.cn/g?b=qq&nk={uid}&s=640"
        return _download_image(url)

    # ---------------------- 渲染 ----------------------

    def render_sign_image(self, data: dict, style: int) -> Optional[bytes]:
        """渲染签到图片，返回 PNG bytes；失败返回 None"""
        uid = int(data.get("uid", 0))
        nickname = str(data.get("nickname", str(uid)))
        inc = int(data.get("inc", 0))
        gold = int(data.get("gold", 0))
        exp = int(data.get("exp", 0))
        rank = int(data.get("rank", 0))
        sign_rank_today = int(data.get("sign_rank_today", 0))
        total_sign_today = int(data.get("total_sign_today", 0))
        total_sign_days = int(data.get("total_sign_days", 0))

        bg_image = self._download_bg()
        avatar_image = self._download_avatar(uid)

        sc_data = ScData(
            uid=uid, nickname=nickname, inc=inc, score=gold, level=exp, rank=rank,
            bg_image=bg_image, avatar_image=avatar_image,
            total_sign_today=total_sign_today,
            sign_rank_today=sign_rank_today,
            total_sign_days=total_sign_days,
        )

        styles = self._get_styles()
        if style < 0 or style >= len(styles):
            style = 1
        draw_fn = styles[style]

        try:
            return draw_fn(sc_data)
        except Exception as e:
            logger.exception("签到图生成失败: %s", e)
            return None

    def render_rank_image(self, rank_list: list) -> Optional[bytes]:
        """渲染等级排行榜（走 image_renderer._render_list_image 榜单渲染）"""
        if not rank_list:
            return None
        mod = _mod()
        if mod is None or not hasattr(mod, "_render_list_image"):
            return None
        try:
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
                "item_size": 20, "title_size": 26,
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
            image = mod._render_list_image(
                f"等级排行榜 TOP{len(rank_list)}", items, 640, 30, options
            )
            if image is None:
                return None
            if isinstance(image, (bytes, bytearray)):
                return bytes(image)
            import io as _io
            buf = _io.BytesIO()
            image.save(buf, format="PNG")
            return buf.getvalue()
        except Exception as e:
            logger.exception("排行榜图生成失败: %s", e)
            return None


# ============================== 4种预设样式绘制（Canvas） ==============================

def _canvas(w, h, bg, font_path=None):
    """创建 Canvas（原生优先，PIL 回退）"""
    mod = _mod()
    if mod is None or not hasattr(mod, "_get_native_or_pil_canvas"):
        raise RuntimeError("image_renderer 未加载，无法渲染签到图")
    return mod._get_native_or_pil_canvas(w, h, bg, font_path or _font_path())


def _paste_bg(canvas, bg_bytes, w, h):
    """背景拉伸铺满画布；无背景则纯色底"""
    if bg_bytes:
        try:
            canvas.paste(bg_bytes, 0, 0, w, h)
            return
        except Exception as e:
            logger.debug("背景粘贴失败，使用纯色底: %s", e)
    canvas.rect(0, 0, w, h, radius=0, fill=(135, 206, 235))


def _circle_avatar(canvas, avatar_bytes, cx, cy, size):
    """粘贴圆形头像（居中于 cx,cy）"""
    if not avatar_bytes:
        return
    mod = _mod()
    try:
        if mod is not None and hasattr(mod, "image_circle_crop"):
            circ = mod.image_circle_crop(avatar_bytes, size)
            canvas.paste(circ, cx - size // 2, cy - size // 2, size, size)
        else:
            canvas.paste(avatar_bytes, cx - size // 2, cy - size // 2, size, size)
    except Exception as e:
        logger.debug("头像粘贴失败: %s", e)


def _progress_bar(canvas, x, y, w, h, ratio, color_a=(0, 255, 0), color_b=(0, 0, 255)):
    """渐变进度条（两色线性渐变，简化原三段渐变）"""
    canvas.rect(x, y, x + w, y + h, radius=h // 2, fill=(150, 150, 150))
    filled = int(w * max(0.0, min(1.0, ratio)))
    if filled > 0:
        canvas.gradient_rect(x, y, x + filled, y + h, color_a, color_b, "horizontal")


def draw_score_15(a: ScData) -> bytes:
    """样式0：简约底栏式，无头像，白色扩展区域"""
    W, H = 800, 765
    canvas = _canvas(W, H, (255, 255, 255))
    _paste_bg(canvas, a.bg_image, W, 450)

    now = datetime.now()
    month_word = now.strftime("%m/%d")
    hour_word = get_hour_word(now)

    canvas.text(80, 540, hour_word, font_size=80, color=(255, 255, 255))
    canvas.text(480, 540, month_word, font_size=80, color=(255, 255, 255))

    y_base = 585
    canvas.text(80, y_base, f"{a.nickname} 金币+{a.inc}", font_size=32, color=(255, 255, 255))
    canvas.text(80, y_base + 45, f"当前金币:{a.score}", font_size=32, color=(255, 255, 255))
    canvas.text(80, y_base + 90, f"LEVEL:{a.rank}", font_size=32, color=(255, 255, 255))

    nextrank_score = rankArray[a.rank + 1] if a.rank < 10 else rankArray[-1]
    _progress_bar(canvas, 80, 698, 480, 45, a.level / max(1, nextrank_score))
    canvas.text(600, 729, f"{a.level}/{nextrank_score}", font_size=32, color=(255, 255, 255))

    return bytes(canvas.to_png())


def draw_score_16(a: ScData) -> bytes:
    """样式1：Aero毛玻璃卡片，圆形头像200x200，渐变进度条"""
    W, H = 800, 450
    canvas = _canvas(W, H, (255, 255, 255))
    _paste_bg(canvas, a.bg_image, W, H)

    # Aero 毛玻璃：整图模糊 + 卡片区域深色半透明覆盖
    try:
        canvas.blur(10)
    except Exception as e:
        logger.debug("模糊失败: %s", e)
    canvas.alpha_overlay(100, 100, 700, 350, (15, 20, 35), 160)

    now = datetime.now()
    hour_word = get_hour_word(now)

    # 圆形头像 200x200
    _circle_avatar(canvas, a.avatar_image, 220, 220, 200)

    canvas.text(350, 180, a.nickname, font_size=50, color=(255, 255, 255))
    canvas.text(350, 280, hour_word, font_size=30, color=(255, 255, 255))
    canvas.text(350, 340, f"金币 + {a.inc}", font_size=30, color=(255, 255, 255))
    canvas.text(350, 390, f"当前金币：{a.score}", font_size=30, color=(255, 255, 255))
    canvas.text(350, 440, f"LEVEL: {a.rank}", font_size=30, color=(255, 255, 255))

    # 右侧统计
    right_x = W - 320
    canvas.text(right_x, 180, f"累计签到 {a.total_sign_days} 天", font_size=24, color=(200, 210, 230))
    if a.sign_rank_today > 0:
        canvas.text(right_x, 220, f"今日第 {a.sign_rank_today} 个签到", font_size=24, color=(200, 210, 230))
    canvas.text(right_x, 260, f"今日共 {a.total_sign_today} 人签到", font_size=24, color=(200, 210, 230))

    canvas.text(120, 330, now.strftime("%Y-%m-%d %H:%M:%S"), font_size=20, color=(200, 210, 230))

    nextrank_score = rankArray[a.rank + 1] if a.rank < 10 else rankArray[-1]
    canvas.text(W - 320, 330, f"{a.level}/{nextrank_score}", font_size=24, color=(200, 210, 230))

    # 渐变进度条
    _progress_bar(canvas, 120, 370, W - 240, 8, a.level / max(1, nextrank_score))

    canvas.text(W // 2 - 100, 420, "AstrBot FunScore", font_size=20, color=(150, 160, 180))
    return bytes(canvas.to_png())


def draw_score_17(a: ScData) -> bytes:
    """样式2：多Aero小卡片，头像100x100"""
    W, H = 800, 450
    canvas = _canvas(W, H, (255, 255, 255))
    _paste_bg(canvas, a.bg_image, W, H)

    def aero_box(x, y, w, h):
        """半透明白色卡片区域"""
        canvas.alpha_overlay(int(x), int(y), int(x + w), int(y + h), (255, 255, 255), 140)
        canvas.rect(int(x), int(y), int(x + w), int(y + h), radius=8, outline=(255, 255, 255), width=2)

    # 昵称卡片（左上）、左下信息、右下时间
    aero_box(20, 40, 280, 100)
    aero_box(20, H - 120, 280, 100)
    aero_box(W - 272, H - 60, 252, 40)

    now = datetime.now()
    hour_word = get_hour_word(now)

    _circle_avatar(canvas, a.avatar_image, 80, 90, 100)

    canvas.text(140, 80, a.nickname, font_size=24, color=(255, 255, 255))
    canvas.text(140, 120, hour_word, font_size=24, color=(255, 255, 255))

    canvas.text(40, H - 90, f"金币 + {a.inc}", font_size=20, color=(255, 255, 255))
    canvas.text(40, H - 60, f"当前金币：{a.score}", font_size=20, color=(255, 255, 255))
    canvas.text(40, H - 30, f"LEVEL: {a.rank}", font_size=20, color=(255, 255, 255))

    canvas.text(W - 260, H - 50, now.strftime("%Y-%m-%d %H:%M:%S"), font_size=20, color=(255, 255, 255))

    nextrank_score = rankArray[a.rank + 1] if a.rank < 10 else rankArray[-1]
    canvas.text(190, H - 30, f"{a.level}/{nextrank_score}", font_size=20, color=(255, 255, 255))
    canvas.text(W // 2 - 100, H - 20, "Created By AstrBot FunScore", font_size=20, color=(255, 255, 255))
    return bytes(canvas.to_png())


def draw_score_17b2(a: ScData) -> bytes:
    """样式3：现代卡片式，主题色提取，阴影，描边（Canvas 简化版）"""
    W, H = 1280, 720
    canvas = _canvas(W, H, (0, 0, 0))
    _paste_bg(canvas, a.bg_image, W, H)

    # 模糊背景
    try:
        canvas.blur(20)
    except Exception as e:
        logger.debug("模糊失败: %s", e)

    theme_color = (100, 150, 200)

    # 右侧主卡片（圆角，主题色半透明 + 白色描边）
    card_x0, card_y0, card_x1, card_y1 = 469, 144, 1237, 576
    canvas.alpha_overlay(card_x0, card_y0, card_x1, card_y1, theme_color, 120)
    canvas.rect(card_x0, card_y0, card_x1, card_y1, radius=12, outline=(255, 255, 255), width=3)

    now = datetime.now()
    hour_word = get_hour_word(now)
    sch = H * 6 // 10

    # 头像 108x108
    aw = 108
    _circle_avatar(canvas, a.avatar_image, 72, 72, aw)

    # 昵称背景 + 昵称
    canvas.rect(72 + aw // 2, 54, 72 + aw // 2 + aw // 40 * 5 + len(a.nickname) * 20, 90,
                radius=8, fill=theme_color)
    canvas.text(72 + aw // 2 + aw // 40 * 2, 54, a.nickname, font_size=36, color=(255, 255, 255))

    # 日期
    canvas.text(W - W // 6, H // 2 - sch // 2 - 20, now.strftime("%Y/%m/%d"),
                font_size=40, color=(255, 255, 255))

    # 等级信息
    nextrank_score = rankArray[a.rank + 1] if a.rank < 10 else rankArray[-1]
    canvas.text(W // 3 * 2 - 100, H // 2 + sch // 2, f"Level {a.rank}", font_size=36, color=(255, 255, 255))
    canvas.text(W // 3 * 2 + 100, H // 2 + sch // 2, f"{a.level}/{nextrank_score}", font_size=36, color=(255, 255, 255))

    # 问候语 + 金币信息
    info_x = int(((W - 100) - (W // 3 - 50)) / 8)
    canvas.text(info_x, (H - sch) // 2 + sch // 4, hour_word, font_size=30, color=(255, 255, 255))
    canvas.text(info_x, (H - sch) // 2 + sch // 4 + 30, f"金币 + {a.inc}", font_size=24, color=(255, 255, 255))
    canvas.text(info_x, (H - sch) // 2 + sch // 4 + 60, "EXP + 1", font_size=24, color=(255, 255, 255))
    canvas.text(info_x, (H - sch) // 2 + sch // 4 * 3, f"你有 {a.score} 枚金币", font_size=28, color=(255, 255, 255))

    canvas.text(4, H - 20, "Create By AstrBot FunScore", font_size=20, color=(255, 255, 255))
    return bytes(canvas.to_png())

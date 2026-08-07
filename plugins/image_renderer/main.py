"""
图片渲染器插件 - 通用图片渲染引擎
提供可复用的图片绘制工具，供其他插件调用生成图片消息。
同时提供 /render_card 命令用于测试渲染效果。

渲染引擎自动选择（按平台）：
  1. 原生扩展 zcbot_render（Rust + pyo3，Windows .pyd / Linux .so，放在 native/bin/<平台>/）
     → 内存增量 <5MB，无子进程开销
  2. 找不到原生扩展时回退 PIL（Pillow），功能一致

命令：
  /render_card [标题] [内容]  生成一张信息卡片图片
  /render_text [文字]         将文字渲染为图片

依赖：
  Pillow>=10.0.0（PIL 回退用）
"""
import importlib.util
import logging
import os
import sys
import tempfile
from datetime import datetime

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "图片渲染器",
    "version": "1.1.0",
    "author": "ZGRIC",
    "desc": "通用图片渲染引擎（原生 Rust 扩展，自动回退 PIL），提供卡片/文本绘制工具",
    "priority": 200,
}

_FONT_DIR = os.path.dirname(os.path.abspath(__file__))
# 字体候选：插件目录 / help 插件自带字体
_FONT_CANDIDATES = [
    os.path.join(_FONT_DIR, 'HarmonyOS_Sans_SC_Regular.ttf'),
    os.path.join(_FONT_DIR, 'NotoSansCJK-Regular.ttc'),
    os.path.join(os.path.dirname(_FONT_DIR), 'help', 'DouyinSansBold.otf'),
]

# 模块级缓存，避免重复加载字体
_FONT_CACHE = {}


def _load_native_renderer():
    """按平台自动加载原生渲染扩展；找不到返回 None（回退 PIL）"""
    native_dir = os.path.join(_FONT_DIR, 'native', 'bin')
    if sys.platform.startswith('win'):
        subdirs = ['win64', 'win-amd64']
        names = ['zcbot_render.pyd', 'zcbot_render.abi3.pyd']
    elif sys.platform.startswith('linux'):
        subdirs = ['linux-aarch64', 'linux64', 'linux-x86_64']
        names = ['zcbot_render.so', 'zcbot_render.abi3.so']
    else:
        return None
    for sub in subdirs:
        for name in names:
            path = os.path.join(native_dir, sub, name)
            if not os.path.isfile(path):
                continue
            try:
                spec = importlib.util.spec_from_file_location('zcbot_render', path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                logger.info(f"[image_renderer] 原生渲染扩展已加载: {path}")
                return mod
            except Exception as e:
                logger.warning(f"[image_renderer] 原生扩展加载失败，回退 PIL: {path} - {e}")
    return None


_NATIVE = _load_native_renderer()


def _find_font_path():
    """查找可用于原生渲染的字体文件路径"""
    for p in _FONT_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def _get_font(size, bold=False):
    """加载字体（带缓存），找不到则用默认（PIL 回退用）"""
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    from PIL import ImageFont
    font_names = ["DouyinSansBold.otf", "HarmonyOS_Sans_SC_Regular.ttf", "NotoSansCJK-Regular.ttc"]
    for fn in font_names:
        fp = os.path.join(_FONT_DIR, fn)
        if os.path.isfile(fp):
            try:
                f = ImageFont.truetype(fp, size)
                _FONT_CACHE[key] = f
                return f
            except Exception:
                continue
    try:
        f = ImageFont.load_default()
        _FONT_CACHE[key] = f
        return f
    except Exception:
        return None


def register(ctx):
    ctx.command(
        "/render_card",
        handle_render_card,
        priority=200,
        description="生成信息卡片图片，用法: /render_card 标题 | 内容",
    )
    ctx.command(
        "/render_text",
        handle_render_text,
        priority=200,
        description="将文字渲染为图片，用法: /render_text 要显示的文字",
    )


def handle_render_card(event, match):
    """生成信息卡片"""
    text = (match.group(1) or event.message or "").strip()
    if not text:
        ctx.send_msg(
            user_id=event.user_id, group_id=event.group_id,
            message="请提供内容，如: /render_card 标题 | 内容",
        )
        return
    parts = [p.strip() for p in text.split("|", 1)]
    title = parts[0] if len(parts) > 0 else "信息卡片"
    content = parts[1] if len(parts) > 1 else title

    result = _render_card_image(title, content)
    _send_image(ctx, event, result)


def handle_render_text(event, match):
    """将文字渲染为图片"""
    text = (match.group(1) or event.message or "").strip()
    if not text:
        ctx.send_msg(
            user_id=event.user_id, group_id=event.group_id,
            message="请提供文字，如: /render_text 你好世界",
        )
        return
    result = _render_text_image(text)
    _send_image(ctx, event, result)


# ---------------------------------------------------------------- 渲染（原生优先，PIL 回退）

def _parse_color(c, default=None):
    """颜色归一化：'#RRGGBB' / '#RRGGBBAA' / [r,g,b] / [r,g,b,a] → RGBA 元组"""
    if c is None:
        return default
    if isinstance(c, str):
        h = c.lstrip('#')
        if len(h) == 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
        if len(h) == 8:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16))
        raise ValueError(f"颜色格式非法: {c}")
    seq = tuple(int(x) for x in c)
    if len(seq) == 3:
        return seq + (255,)
    if len(seq) == 4:
        return seq
    raise ValueError("颜色序列长度必须为 3 或 4")


def _gradient_row(top, bottom, y, height):
    """渐变第 y 行的颜色（与原生版 lerp 一致）"""
    t = y / max(1, height - 1)
    return tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,)


def _render_card_image(title, content, width=600, padding=30, options=None):
    """渲染信息卡片。原生可用返回 PNG bytes，否则返回 PIL Image"""
    if _NATIVE is not None:
        font = _find_font_path()
        if font:
            try:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                return _NATIVE.render_card(title, content, font, ts, width, padding, options)
            except Exception as e:
                logger.warning(f"[image_renderer] 原生 render_card 失败，回退 PIL: {e}")
    return _render_card_image_pil(title, content, width, padding, options)


def _render_text_image(text, width=500, padding=20, options=None):
    """将文字渲染为图片。原生可用返回 PNG bytes，否则返回 PIL Image"""
    if _NATIVE is not None:
        font = _find_font_path()
        if font:
            try:
                return _NATIVE.render_text(text, font, width, 24, padding, options)
            except Exception as e:
                logger.warning(f"[image_renderer] 原生 render_text 失败，回退 PIL: {e}")
    return _render_text_image_pil(text, width, padding, options)


def _render_list_image(title, items, width=600, padding=30, options=None):
    """渲染榜单/列表图片。原生可用返回 PNG bytes，否则返回 PIL Image"""
    if _NATIVE is not None:
        font = _find_font_path()
        if font:
            try:
                return _NATIVE.render_list(title, items, font, width, padding, options)
            except Exception as e:
                logger.warning(f"[image_renderer] 原生 render_list 失败，回退 PIL: {e}")
    return _render_list_image_pil(title, items, width, padding, options)


# ---------------------------------------------------------------- PIL 回退实现

def _render_card_image_pil(title, content, width=600, padding=30, options=None):
    """PIL 版信息卡片渲染（与原生版布局一致，支持相同 options）"""
    from PIL import Image, ImageDraw

    options = options or {}
    padding = int(options.get('padding', padding))
    title_size = int(options.get('title_size', 28))
    content_size = int(options.get('content_size', 20))
    footer_size = int(options.get('footer_size', 14))
    line_h = int(options.get('line_height', 0)) or 30
    align = options.get('align', 'left')
    radius = int(options.get('radius', 0))
    border_color = _parse_color(options.get('border_color'))
    border_width = int(options.get('border_width', 2))

    title_color = _parse_color(options.get('title_color'), (20, 30, 60, 255))
    content_color = _parse_color(options.get('content_color'), (60, 60, 80, 255))
    footer_color = _parse_color(options.get('footer_color'), (160, 160, 170, 255))
    accent_color = _parse_color(options.get('accent_color'), (99, 102, 241, 255))
    bg_color = _parse_color(options.get('bg_color'))
    bg_gradient = options.get('bg_gradient')
    show_footer = bool(options.get('show_footer', True))
    footer_text = options.get('footer_text', 'ZGRIC')

    title_font = _get_font(title_size, bold=True)
    content_font = _get_font(content_size)
    footer_font = _get_font(footer_size)

    line_height = line_h
    content_lines = []
    for line in content.split("\n"):
        if content_font:
            avg_char_w = content_font.getlength("中")
            chars_per_line = max(1, int((width - padding * 2) / avg_char_w))
            for i in range(0, len(line), chars_per_line):
                content_lines.append(line[i:i + chars_per_line])
        else:
            content_lines.append(line)

    title_h = 50
    content_h = len(content_lines) * line_height + 20
    footer_h = 30 if show_footer else 0
    total_h = padding * 2 + title_h + content_h + footer_h

    img = Image.new("RGBA", (width, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 背景：渐变 / 纯色 / 默认
    gradient = None
    if bg_gradient:
        top = _parse_color(bg_gradient[0], (248, 250, 255, 255))
        bottom = _parse_color(bg_gradient[1], (255, 255, 245, 255))
        gradient = (top, bottom)
    elif bg_color is None:
        gradient = ((248, 250, 255, 255), (255, 255, 245, 255))
    if gradient:
        top, bottom = gradient
        for y in range(total_h):
            draw.line([(0, y), (width, y)], fill=_gradient_row(top, bottom, y, total_h))
    else:
        if radius > 0:
            draw.rounded_rectangle([0, 0, width - 1, total_h - 1], radius=radius, fill=bg_color)
        else:
            draw.rectangle([0, 0, width - 1, total_h - 1], fill=bg_color)

    # 边框
    if border_color:
        box = [0, 0, width - 1, total_h - 1]
        if radius > 0:
            draw.rounded_rectangle(box, radius=radius, outline=border_color, width=border_width)
        else:
            draw.rectangle(box, outline=border_color, width=border_width)

    # 标题栏左侧彩色条
    draw.rectangle([padding, padding, padding + 6, padding + title_h], fill=accent_color)

    # 标题
    if title_font:
        draw.text((padding + 18, padding + 4), title, fill=title_color, font=title_font)

    # 内容（支持对齐）
    y_off = padding + title_h + 10
    content_x0 = padding + 6
    content_x1 = width - padding - 6
    inner_w = content_x1 - content_x0
    if content_font:
        for line in content_lines:
            lw = content_font.getlength(line)
            if align == 'center':
                x = content_x0 + max(0, inner_w - lw) / 2
            elif align == 'right':
                x = content_x1 - min(lw, inner_w)
            else:
                x = content_x0
            draw.text((x, y_off), line, fill=content_color, font=content_font)
            y_off += line_height

    # 页脚：左时间戳，右 footer_text
    if show_footer and footer_font:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        foot_y = total_h - padding - footer_h + 8
        draw.text((padding, foot_y), ts, fill=footer_color, font=footer_font)
        if footer_text:
            footer_text = str(footer_text)
            tw = footer_font.getlength(footer_text)
            draw.text((width - padding - tw, foot_y), footer_text, fill=footer_color, font=footer_font)

    # 圆角裁剪（渐变背景时角落透明）
    if radius > 0 and gradient:
        mask = Image.new("L", (width, total_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, width - 1, total_h - 1], radius=radius, fill=255
        )
        img.putalpha(mask)
    return img


def _render_text_image_pil(text, width=500, padding=20, options=None):
    """PIL 版文字渲染为图片（与原生版布局一致，支持相同 options）"""
    from PIL import Image, ImageDraw

    options = options or {}
    padding = int(options.get('padding', padding))
    font_size = int(options.get('font_size', 24))
    line_h = int(options.get('line_height', 0)) or max(1, round(font_size * 1.35))
    align = options.get('align', 'left')
    radius = int(options.get('radius', 0))
    border_color = _parse_color(options.get('border_color'))
    border_width = int(options.get('border_width', 2))
    text_color = _parse_color(options.get('text_color'), (40, 40, 60, 255))
    bg_color = _parse_color(options.get('bg_color'), (248, 250, 255, 255))
    bg_gradient = options.get('bg_gradient')

    font = _get_font(font_size)
    line_height = line_h

    lines = []
    for para in text.split("\n"):
        if font:
            avg_char_w = font.getlength("中")
            chars_per_line = max(1, int((width - padding * 2) / avg_char_w))
            for i in range(0, len(para), chars_per_line):
                lines.append(para[i:i + chars_per_line])
        else:
            lines.append(para)

    total_h = padding * 2 + len(lines) * line_height + 20
    img = Image.new("RGBA", (width, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 背景：渐变 / 纯色
    gradient = False
    if bg_gradient:
        top = _parse_color(bg_gradient[0], bg_color)
        bottom = _parse_color(bg_gradient[1], bg_color)
        for y in range(total_h):
            draw.line([(0, y), (width, y)], fill=_gradient_row(top, bottom, y, total_h))
        gradient = True
    else:
        if radius > 0:
            draw.rounded_rectangle([0, 0, width - 1, total_h - 1], radius=radius, fill=bg_color)
        else:
            draw.rectangle([0, 0, width - 1, total_h - 1], fill=bg_color)

    # 边框
    if border_color:
        box = [0, 0, width - 1, total_h - 1]
        if radius > 0:
            draw.rounded_rectangle(box, radius=radius, outline=border_color, width=border_width)
        else:
            draw.rectangle(box, outline=border_color, width=border_width)

    # 文字（支持对齐）
    y_off = padding
    inner_w = width - padding * 2
    if font:
        for line in lines:
            lw = font.getlength(line)
            if align == 'center':
                x = padding + max(0, inner_w - lw) / 2
            elif align == 'right':
                x = padding + inner_w - min(lw, inner_w)
            else:
                x = padding
            draw.text((x, y_off), line, fill=text_color, font=font)
            y_off += line_height

    # 圆角裁剪
    if radius > 0 and gradient:
        mask = Image.new("L", (width, total_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, width - 1, total_h - 1], radius=radius, fill=255
        )
        img.putalpha(mask)
    return img


def _render_list_image_pil(title, items, width=600, padding=30, options=None):
    """PIL 版榜单/列表渲染（与原生版布局一致，支持相同 options）"""
    from PIL import Image, ImageDraw

    options = options or {}
    padding = int(options.get('padding', padding))
    title_size = int(options.get('title_size', 24))
    item_size = int(options.get('item_size', 18))
    line_h = int(options.get('line_height', 0)) or max(1, round(item_size * 1.6))
    radius = int(options.get('radius', 0))
    border_color = _parse_color(options.get('border_color'))
    border_width = int(options.get('border_width', 2))

    title_color = _parse_color(options.get('title_color'), (20, 30, 60, 255))
    accent_color = _parse_color(options.get('accent_color'), (99, 102, 241, 255))
    name_color = _parse_color(options.get('name_color'), (40, 40, 60, 255))
    value_color = _parse_color(options.get('value_color'), (120, 120, 140, 255))
    highlight_bg = _parse_color(options.get('highlight_bg'), (236, 239, 255, 255))
    highlight_color = _parse_color(options.get('highlight_color'), (99, 102, 241, 255))
    rank_color = _parse_color(options.get('rank_color'), (160, 160, 170, 255))
    bg_color = _parse_color(options.get('bg_color'))
    bg_gradient = options.get('bg_gradient')

    title_font = _get_font(title_size, bold=True)
    item_font = _get_font(item_size)

    title_h = 56
    row_h = line_h + 4
    total_h = padding * 2 + title_h + len(items) * row_h + 10

    img = Image.new("RGBA", (width, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 背景：渐变 / 纯色 / 默认渐变
    gradient = False
    if bg_gradient:
        top = _parse_color(bg_gradient[0], (248, 250, 255, 255))
        bottom = _parse_color(bg_gradient[1], (255, 255, 245, 255))
        for y in range(total_h):
            draw.line([(0, y), (width, y)], fill=_gradient_row(top, bottom, y, total_h))
        gradient = True
    elif bg_color is None:
        top, bottom = (248, 250, 255, 255), (255, 255, 245, 255)
        for y in range(total_h):
            draw.line([(0, y), (width, y)], fill=_gradient_row(top, bottom, y, total_h))
        gradient = True
    else:
        if radius > 0:
            draw.rounded_rectangle([0, 0, width - 1, total_h - 1], radius=radius, fill=bg_color)
        else:
            draw.rectangle([0, 0, width - 1, total_h - 1], fill=bg_color)

    # 边框
    if border_color:
        box = [0, 0, width - 1, total_h - 1]
        if radius > 0:
            draw.rounded_rectangle(box, radius=radius, outline=border_color, width=border_width)
        else:
            draw.rectangle(box, outline=border_color, width=border_width)

    # 标题区：左侧彩色条 + 标题
    draw.rectangle([padding, padding, padding + 6, padding + title_h], fill=accent_color)
    if title_font:
        draw.text((padding + 18, padding + 4), title, fill=title_color, font=title_font)

    # 行内容
    x0, x1 = padding, width - padding
    rank_w = 44
    y_off = padding + title_h + 8
    if item_font:
        for item in items:
            if isinstance(item, str):
                row = {'name': item, 'value': '', 'rank': None, 'highlight': False}
            else:
                row = {
                    'name': str(item.get('name', '')),
                    'value': str(item.get('value', '')) if item.get('value') is not None else '',
                    'rank': str(item.get('rank')) if item.get('rank') is not None else None,
                    'highlight': bool(item.get('highlight', False)),
                }
            # 高亮整行背景
            if row['highlight']:
                draw.rectangle([x0, y_off, x1, y_off + row_h], fill=highlight_bg)

            baseline = y_off + item_size  # 与原生版 baseline 近似
            # 序号（右对齐到序号区）
            if row['rank']:
                draw.text((x0 + rank_w - item_font.getlength(row['rank']), y_off),
                          row['rank'], fill=rank_color, font=item_font)
            # 名称
            name_color_cur = highlight_color if row['highlight'] else name_color
            draw.text((x0 + rank_w, y_off), row['name'], fill=name_color_cur, font=item_font)
            # 数值（右对齐）
            if row['value']:
                vw = item_font.getlength(row['value'])
                draw.text((x1 - min(vw, x1 - x0 - rank_w), y_off),
                          row['value'], fill=value_color, font=item_font)

            y_off += row_h

    # 圆角裁剪
    if radius > 0 and gradient:
        mask = Image.new("L", (width, total_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, width - 1, total_h - 1], radius=radius, fill=255
        )
        img.putalpha(mask)
    return img


def _send_image(ctx, event, img_or_bytes):
    """发送图片（支持 PIL Image 或 PNG bytes），发送后自动清理"""
    img_path = None
    try:
        is_bytes = isinstance(img_or_bytes, (bytes, bytearray))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            img_path = tmp.name
            if is_bytes:
                tmp.write(bytes(img_or_bytes))
            else:
                img_or_bytes.save(tmp.name, "PNG")

        # Windows 临时路径含反斜杠, 需转为正斜杠才能被 OneBot 客户端解析
        path_str = img_path.replace("\\", "/")
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id,
            message=f"[CQ:image,file=file:///{path_str}]",
        )
    except Exception as e:
        ctx.log(f"发送图片失败: {e}", level="error")
        ctx.send_msg(
            user_id=event.user_id, group_id=event.group_id,
            message=f"图片生成失败: {e}",
        )
    finally:
        if img_path:
            try:
                os.unlink(img_path)
            except Exception:
                pass

//! zcbot_render — ZCBOT 原生图片渲染扩展（pyo3 + fontdue + image）
//!
//! 提供两个函数，输出 PNG bytes：
//!   - render_text(text, font_path, width=500, font_size=24, padding=20, options=None)
//!   - render_card(title, content, font_path, timestamp, width=600, padding=30, options=None)
//!
//! `options` 为可选 dict，与 PIL 回退版参数完全一致，键如下（缺省即默认值）：
//!   颜色（"#RRGGBB" / "#RRGGBBAA" / [r,g,b] / [r,g,b,a]）：
//!     text_color / title_color / content_color / footer_color / accent_color
//!     bg_color（纯色背景） / bg_gradient（[顶部色, 底部色] 垂直渐变，优先于 bg_color）
//!     border_color（边框颜色，默认 None）
//!   字号：font_size / title_size / content_size / footer_size
//!   布局：line_height（0=自动）、padding、align（"left"/"center"/"right"）
//!   样式：radius（圆角像素）、border_width（边框宽，默认 2）
//!   卡片页脚：show_footer（默认 true）、footer_text（默认 "ZGRIC"，None 则不显示右侧文字）
//!
//! 使用 abi3（稳定 ABI）编译：Windows 产出 zcbot_render.pyd，Linux 产出 zcbot_render.so，
//! 一份扩展兼容 Python 3.9+，由 image_renderer 插件按平台自动加载。

use fontdue::{Font, FontSettings};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

// ---------------------------------------------------------------- 颜色

#[derive(Clone, Copy)]
struct Style {
    text_color: [u8; 4],
    title_color: [u8; 4],
    content_color: [u8; 4],
    footer_color: [u8; 4],
    accent_color: [u8; 4],
    bg_color: Option<[u8; 4]>,
    bg_gradient: Option<([u8; 4], [u8; 4])>,
    border_color: Option<[u8; 4]>,
    border_width: u32,
    radius: u32,
    align: Align,
}

impl Default for Style {
    fn default() -> Self {
        Style {
            text_color: [40, 40, 60, 255],
            title_color: [20, 30, 60, 255],
            content_color: [60, 60, 80, 255],
            footer_color: [160, 160, 170, 255],
            accent_color: [99, 102, 241, 255],
            bg_color: None,
            bg_gradient: None,
            border_color: None,
            border_width: 2,
            radius: 0,
            align: Align::Left,
        }
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Align {
    Left,
    Center,
    Right,
}

fn parse_color(obj: &Bound<'_, PyAny>) -> PyResult<Option<[u8; 4]>> {
    if obj.is_none() {
        return Ok(None);
    }
    // 字符串 "#RRGGBB" / "#RRGGBBAA"
    if let Ok(s) = obj.extract::<String>() {
        let hex = s.trim_start_matches('#');
        let len = hex.len();
        let parse = |i: usize| -> PyResult<u8> {
            u8::from_str_radix(&hex[i..i + 2], 16)
                .map_err(|_| PyRuntimeError::new_err(format!("颜色格式非法: {s}")))
        };
        let rgba = match len {
            6 => [parse(0)?, parse(2)?, parse(4)?, 255],
            8 => [parse(0)?, parse(2)?, parse(4)?, parse(6)?],
            _ => return Err(PyRuntimeError::new_err(format!("颜色格式非法: {s}"))),
        };
        return Ok(Some(rgba));
    }
    // 序列 [r,g,b] / [r,g,b,a]
    if let Ok(v) = obj.extract::<Vec<i64>>() {
        let clamp = |n: i64| n.clamp(0, 255) as u8;
        return match v.len() {
            3 => Ok(Some([clamp(v[0]), clamp(v[1]), clamp(v[2]), 255])),
            4 => Ok(Some([clamp(v[0]), clamp(v[1]), clamp(v[2]), clamp(v[3])])),
            _ => Err(PyRuntimeError::new_err("颜色序列长度必须为 3 或 4")),
        };
    }
    Err(PyRuntimeError::new_err(
        "颜色必须是 '#RRGGBB' / '#RRGGBBAA' 或 [r,g,b] / [r,g,b,a]",
    ))
}

fn parse_gradient(obj: &Bound<'_, PyAny>) -> PyResult<Option<([u8; 4], [u8; 4])>> {
    if obj.is_none() {
        return Ok(None);
    }
    let items: Vec<Bound<'_, PyAny>> = obj
        .try_iter()
        .map_err(|_| PyRuntimeError::new_err("bg_gradient 必须是两个颜色的序列"))?
        .collect::<PyResult<_>>()
        .map_err(|_| PyRuntimeError::new_err("bg_gradient 必须是两个颜色的序列"))?;
    if items.len() != 2 {
        return Err(PyRuntimeError::new_err(
            "bg_gradient 必须是两个颜色的序列 [顶部色, 底部色]",
        ));
    }
    let top = parse_color(&items[0])?.ok_or_else(|| PyRuntimeError::new_err("bg_gradient 颜色不能为空"))?;
    let bottom = parse_color(&items[1])?.ok_or_else(|| PyRuntimeError::new_err("bg_gradient 颜色不能为空"))?;
    Ok(Some((top, bottom)))
}

fn parse_align(obj: &Bound<'_, PyAny>) -> PyResult<Align> {
    let s: String = obj
        .extract()
        .map_err(|_| PyRuntimeError::new_err("align 必须是字符串"))?;
    match s.as_str() {
        "left" => Ok(Align::Left),
        "center" => Ok(Align::Center),
        "right" => Ok(Align::Right),
        _ => Err(PyRuntimeError::new_err(
            "align 只能是 'left' / 'center' / 'right'",
        )),
    }
}

// ---------------------------------------------------------------- options 解析

struct Opts {
    font_size: Option<u32>,
    title_size: Option<u32>,
    content_size: Option<u32>,
    footer_size: Option<u32>,
    line_height: Option<u32>,
    padding: Option<u32>,
    style: Style,
    show_footer: bool,
    footer_text: Option<String>,
}

fn get_opt<'py>(d: &Bound<'py, PyDict>, key: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    Ok(d.get_item(key)?)
}

fn get_u32(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<u32>> {
    match get_opt(d, key)? {
        Some(v) if !v.is_none() => v
            .extract()
            .map(Some)
            .map_err(|_| PyRuntimeError::new_err(format!("{key} 必须是整数"))),
        _ => Ok(None),
    }
}

fn get_bool(d: &Bound<'_, PyDict>, key: &str, default: bool) -> PyResult<bool> {
    match get_opt(d, key)? {
        Some(v) if !v.is_none() => v
            .extract()
            .map_err(|_| PyRuntimeError::new_err(format!("{key} 必须是布尔值"))),
        _ => Ok(default),
    }
}

fn get_color(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<[u8; 4]>> {
    match get_opt(d, key)? {
        Some(v) if !v.is_none() => parse_color(&v),
        _ => Ok(None),
    }
}

impl Opts {
    fn parse(options: Option<&Bound<'_, PyDict>>) -> PyResult<Opts> {
        let mut o = Opts {
            font_size: None,
            title_size: None,
            content_size: None,
            footer_size: None,
            line_height: None,
            padding: None,
            style: Style::default(),
            show_footer: true,
            footer_text: None,
        };
        if let Some(d) = options {
            o.font_size = get_u32(d, "font_size")?;
            o.title_size = get_u32(d, "title_size")?;
            o.content_size = get_u32(d, "content_size")?;
            o.footer_size = get_u32(d, "footer_size")?;
            o.line_height = get_u32(d, "line_height")?;
            o.padding = get_u32(d, "padding")?;
            o.style.bg_color = get_color(d, "bg_color")?;
            if let Some(v) = get_opt(d, "bg_gradient")? {
                if !v.is_none() {
                    o.style.bg_gradient = parse_gradient(&v)?;
                }
            }
            o.style.border_color = get_color(d, "border_color")?;
            o.style.border_width = get_u32(d, "border_width")?.unwrap_or(2);
            o.style.radius = get_u32(d, "radius")?.unwrap_or(0);
            if let Some(v) = get_opt(d, "align")? {
                if !v.is_none() {
                    o.style.align = parse_align(&v)?;
                }
            }
            o.style.text_color = get_color(d, "text_color")?.unwrap_or(o.style.text_color);
            o.style.title_color = get_color(d, "title_color")?.unwrap_or(o.style.title_color);
            o.style.content_color = get_color(d, "content_color")?.unwrap_or(o.style.content_color);
            o.style.footer_color = get_color(d, "footer_color")?.unwrap_or(o.style.footer_color);
            o.style.accent_color = get_color(d, "accent_color")?.unwrap_or(o.style.accent_color);
            o.show_footer = get_bool(d, "show_footer", true)?;
            o.footer_text = match get_opt(d, "footer_text")? {
                Some(v) if !v.is_none() => Some(
                    v.extract()
                        .map_err(|_| PyRuntimeError::new_err("footer_text 必须是字符串"))?,
                ),
                // 显式 None → 空字符串（隐藏右侧文字）；未传 → None（默认 ZGRIC）
                Some(_) => Some(String::new()),
                None => None,
            };
        }
        Ok(o)
    }
}

// ---------------------------------------------------------------- 字体 & 排版

fn load_font(path: &str) -> Result<Font, String> {
    let data = std::fs::read(path).map_err(|e| format!("读取字体失败: {e}"))?;
    Font::from_bytes(data, FontSettings::default()).map_err(|e| format!("解析字体失败: {e}"))
}

/// 按字符度量宽度换行（CJK 友好），保持与 PIL 版行为一致
fn wrap_lines(font: &Font, text: &str, size: f32, max_w: f32) -> Vec<String> {
    let mut lines: Vec<String> = Vec::new();
    for para in text.split('\n') {
        let mut line = String::new();
        let mut w: f32 = 0.0;
        for ch in para.chars() {
            let (m, _) = font.rasterize(ch, size);
            let cw = m.advance_width;
            if w + cw > max_w && !line.is_empty() {
                lines.push(std::mem::take(&mut line));
                w = 0.0;
            }
            w += cw;
            line.push(ch);
        }
        lines.push(line);
    }
    if lines.is_empty() {
        lines.push(String::new());
    }
    lines
}

fn measure_text(font: &Font, text: &str, size: f32) -> f32 {
    let mut w = 0.0f32;
    for ch in text.chars() {
        let (m, _) = font.rasterize(ch, size);
        w += m.advance_width;
    }
    w
}

fn set_px(buf: &mut [u8], width: u32, x: i32, y: i32, c: [u8; 4]) {
    if x < 0 || y < 0 || x >= width as i32 {
        return;
    }
    let idx = ((y as u32 * width + x as u32) * 4) as usize;
    if idx + 4 <= buf.len() {
        buf[idx..idx + 4].copy_from_slice(&c);
    }
}

/// 在 (baseline_x, baseline_y) 绘制文本，按 glyph 覆盖率做颜色混合
#[allow(clippy::too_many_arguments)]
fn draw_text(
    buf: &mut [u8],
    width: u32,
    height: u32,
    font: &Font,
    size: f32,
    base_x: i32,
    baseline_y: i32,
    text: &str,
    color: [u8; 4],
) {
    let [r, g, b, a] = color;
    let mut cx = base_x;
    for ch in text.chars() {
        let (m, bitmap) = font.rasterize(ch, size);
        for py in 0..m.height as i32 {
            for px in 0..m.width as i32 {
                let cov = bitmap[(py * m.width as i32 + px) as usize] as u32;
                if cov == 0 {
                    continue;
                }
                let bx = cx + m.xmin + px;
                let by = baseline_y + m.ymin + py;
                if bx < 0 || by < 0 || bx >= width as i32 || by >= height as i32 {
                    continue;
                }
                let idx = ((by as u32 * width + bx as u32) * 4) as usize;
                let src_a = (a as u32 * cov) / 255;
                if src_a >= 255 {
                    buf[idx..idx + 4].copy_from_slice(&[r, g, b, 255]);
                } else if src_a > 0 {
                    let inv = 255 - src_a;
                    buf[idx] = ((buf[idx] as u32 * inv + r as u32 * src_a) / 255) as u8;
                    buf[idx + 1] = ((buf[idx + 1] as u32 * inv + g as u32 * src_a) / 255) as u8;
                    buf[idx + 2] = ((buf[idx + 2] as u32 * inv + b as u32 * src_a) / 255) as u8;
                    buf[idx + 3] = 255;
                }
            }
        }
        cx += m.advance_width as i32;
    }
}

/// 计算行在 [x0, x1] 区间内按对齐方式命中的起点
fn line_x(align: Align, line_w: f32, x0: i32, x1: i32) -> i32 {
    let inner = (x1 - x0) as f32;
    match align {
        Align::Left => x0,
        Align::Center => x0 + ((inner - line_w).max(0.0) / 2.0) as i32,
        Align::Right => (x1 as f32 - line_w).max(x0 as f32) as i32,
    }
}

/// 判断像素是否位于 (x0,y0)-(x1,y1) 圆角矩形内（r 为圆角半径）
fn in_rounded(x: i32, y: i32, x0: i32, y0: i32, x1: i32, y1: i32, r: i32) -> bool {
    if x < x0 || y < y0 || x >= x1 || y >= y1 {
        return false;
    }
    if r <= 0 {
        return true;
    }
    let (cx, cy) = if x < x0 + r && y < y0 + r {
        (x0 + r, y0 + r)
    } else if x >= x1 - r && y < y0 + r {
        (x1 - r - 1, y0 + r)
    } else if x < x0 + r && y >= y1 - r {
        (x0 + r, y1 - r - 1)
    } else if x >= x1 - r && y >= y1 - r {
        (x1 - r - 1, y1 - r - 1)
    } else {
        return true;
    };
    let dx = x - cx;
    let dy = y - cy;
    dx * dx + dy * dy <= r * r
}

fn lerp_color(a: [u8; 4], b: [u8; 4], t: f32) -> [u8; 4] {
    let m = |x: u8, y: u8| (x as f32 + (y as f32 - x as f32) * t).round() as u8;
    [m(a[0], b[0]), m(a[1], b[1]), m(a[2], b[2]), 255]
}

/// 填充背景（纯色或垂直渐变），并按圆角裁剪角落为透明
fn fill_bg(buf: &mut [u8], width: u32, height: u32, style: &Style, solid_fallback: [u8; 4]) {
    let w = width as i32;
    let h = height as i32;
    let r = style.radius as i32;
    let solid = style.bg_color;
    let grad = style.bg_gradient;
    for y in 0..h {
        for x in 0..w {
            if !in_rounded(x, y, 0, 0, w, h, r) {
                continue; // 圆角外保持透明
            }
            let c = match (solid, grad) {
                (Some(c), _) => c,
                (None, Some((t, b))) => {
                    let ratio = if h <= 1 { 0.0 } else { y as f32 / (h - 1) as f32 };
                    lerp_color(t, b, ratio)
                }
                (None, None) => solid_fallback,
            };
            set_px(buf, width, x, y, c);
        }
    }
}

/// 绘制圆角矩形边框
fn draw_border(buf: &mut [u8], width: u32, height: u32, style: &Style) {
    let Some(bc) = style.border_color else { return };
    if style.border_width == 0 {
        return;
    }
    let w = width as i32;
    let h = height as i32;
    let r = style.radius as i32;
    let bw = style.border_width as i32;
    for y in 0..h {
        for x in 0..w {
            let outer = in_rounded(x, y, 0, 0, w, h, r);
            if !outer {
                continue;
            }
            let inner_r = (r - bw).max(0);
            let inner = in_rounded(x, y, bw, bw, w - bw, h - bw, inner_r);
            if !inner {
                set_px(buf, width, x, y, bc);
            }
        }
    }
}

fn encode_png(width: u32, height: u32, buf: Vec<u8>) -> Result<Vec<u8>, String> {
    use image::{ImageBuffer, Rgba};
    let img = ImageBuffer::<Rgba<u8>, Vec<u8>>::from_raw(width, height, buf)
        .ok_or_else(|| "图像尺寸非法".to_string())?;
    let mut out: Vec<u8> = Vec::new();
    {
        let mut cursor = std::io::Cursor::new(&mut out);
        image::DynamicImage::ImageRgba8(img)
            .write_to(&mut cursor, image::ImageFormat::Png)
            .map_err(|e| format!("PNG 编码失败: {e}"))?;
    }
    Ok(out)
}

// ---------------------------------------------------------------- 渲染函数

/// 将文字渲染为图片，返回 PNG bytes
#[pyfunction]
#[pyo3(signature = (text, font_path, width=500, font_size=24, padding=20, options=None))]
fn render_text(
    text: &str,
    font_path: &str,
    width: u32,
    font_size: u32,
    padding: u32,
    options: Option<&Bound<'_, PyDict>>,
) -> PyResult<Vec<u8>> {
    if width == 0 {
        return Err(PyRuntimeError::new_err("width 必须大于 0"));
    }
    let o = Opts::parse(options)?;
    let font_size = o.font_size.unwrap_or(font_size);
    let padding = o.padding.unwrap_or(padding);
    if font_size == 0 {
        return Err(PyRuntimeError::new_err("font_size 必须大于 0"));
    }
    let font = load_font(font_path).map_err(PyRuntimeError::new_err)?;
    let line_h = match o.line_height {
        Some(v) if v > 0 => v,
        _ => (font_size as f32 * 1.35).round().max(1.0) as u32,
    };
    let inner_w = (width as i32 - padding as i32 * 2).max(1) as f32;
    let lines = wrap_lines(&font, text, font_size as f32, inner_w);
    let height = padding * 2 + lines.len() as u32 * line_h + 20;
    let mut buf = vec![0u8; (width * height * 4) as usize];
    fill_bg(&mut buf, width, height, &o.style, [248, 250, 255, 255]);
    draw_border(&mut buf, width, height, &o.style);
    let mut top = padding as i32;
    for line in &lines {
        let lw = measure_text(&font, line, font_size as f32);
        let bx = line_x(
            o.style.align,
            lw,
            padding as i32,
            width as i32 - padding as i32,
        );
        draw_text(
            &mut buf,
            width,
            height,
            &font,
            font_size as f32,
            bx,
            top + font_size as i32,
            line,
            o.style.text_color,
        );
        top += line_h as i32;
    }
    encode_png(width, height, buf).map_err(PyRuntimeError::new_err)
}

/// 渲染信息卡片图片，返回 PNG bytes
#[pyfunction]
#[pyo3(signature = (title, content, font_path, timestamp, width=600, padding=30, options=None))]
#[allow(clippy::too_many_arguments)]
fn render_card(
    title: &str,
    content: &str,
    font_path: &str,
    timestamp: &str,
    width: u32,
    padding: u32,
    options: Option<&Bound<'_, PyDict>>,
) -> PyResult<Vec<u8>> {
    if width == 0 {
        return Err(PyRuntimeError::new_err("width 必须大于 0"));
    }
    let o = Opts::parse(options)?;
    let padding = o.padding.unwrap_or(padding);
    let title_size = o.title_size.unwrap_or(28);
    let content_size = o.content_size.unwrap_or(20);
    let footer_size = o.footer_size.unwrap_or(14);
    if title_size == 0 || content_size == 0 || footer_size == 0 {
        return Err(PyRuntimeError::new_err("字号必须大于 0"));
    }
    let font = load_font(font_path).map_err(PyRuntimeError::new_err)?;
    let line_h = match o.line_height {
        Some(v) if v > 0 => v,
        _ => 30u32,
    };
    let title_h = 50u32;
    let inner_w = (width as i32 - padding as i32 * 2).max(1) as f32;
    let content_lines = wrap_lines(&font, content, content_size as f32, inner_w);
    let content_h = content_lines.len() as u32 * line_h + 20;
    let footer_h = if o.show_footer { 30u32 } else { 0u32 };
    let total_h = padding * 2 + title_h + content_h + footer_h;

    let mut buf = vec![0u8; (width * total_h * 4) as usize];
    // 卡片默认垂直渐变背景（与 PIL 版一致）
    fill_bg(&mut buf, width, total_h, &o.style, [248, 250, 255, 255]);
    if o.style.bg_color.is_none() && o.style.bg_gradient.is_none() {
        // 覆盖默认渐变：顶部浅蓝 → 底部微黄
        let mut g = o.style;
        g.bg_gradient = Some(([248, 250, 255, 255], [255, 255, 245, 255]));
        fill_bg(&mut buf, width, total_h, &g, [248, 250, 255, 255]);
    }
    draw_border(&mut buf, width, total_h, &o.style);

    // 标题栏左侧彩色条
    for y in padding..padding + title_h {
        for x in padding..padding + 6 {
            set_px(&mut buf, width, x as i32, y as i32, o.style.accent_color);
        }
    }

    // 标题
    draw_text(
        &mut buf,
        width,
        total_h,
        &font,
        title_size as f32,
        (padding + 18) as i32,
        (padding + title_size) as i32,
        title,
        o.style.title_color,
    );

    // 内容（支持对齐，与 PIL 版一致：内容区 x 起点为 padding+6）
    let mut top = padding + title_h + 10;
    let content_x0 = (padding + 6) as i32;
    let content_x1 = width as i32 - padding as i32 - 6;
    for line in &content_lines {
        let lw = measure_text(&font, line, content_size as f32);
        let bx = line_x(o.style.align, lw, content_x0, content_x1);
        draw_text(
            &mut buf,
            width,
            total_h,
            &font,
            content_size as f32,
            bx,
            (top + content_size) as i32,
            line,
            o.style.content_color,
        );
        top += line_h;
    }

    // 页脚：左时间戳，右 footer_text（默认 ZGRIC）
    if o.show_footer {
        let foot_y = (total_h - padding - footer_h + 8) as i32;
        draw_text(
            &mut buf,
            width,
            total_h,
            &font,
            footer_size as f32,
            padding as i32,
            foot_y + footer_size as i32,
            timestamp,
            o.style.footer_color,
        );
        let right_text: &str = o.footer_text.as_deref().unwrap_or("ZGRIC");
        if !right_text.is_empty() {
            let z_w = measure_text(&font, right_text, footer_size as f32);
            draw_text(
                &mut buf,
                width,
                total_h,
                &font,
                footer_size as f32,
                (width as i32 - padding as i32 - z_w as i32).max(0),
                foot_y + footer_size as i32,
                right_text,
                o.style.footer_color,
            );
        }
    }

    encode_png(width, total_h, buf).map_err(PyRuntimeError::new_err)
}

#[pymodule]
fn zcbot_render(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(render_text, m)?)?;
    m.add_function(wrap_pyfunction!(render_card, m)?)?;
    Ok(())
}

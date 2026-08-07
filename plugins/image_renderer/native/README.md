# zcbot_render — ZCBOT 原生图片渲染扩展

基于 **Rust + pyo3** 编译的 Python 原生扩展，供 image_renderer 插件使用：
内存增量 <5MB、无子进程开销。abi3 稳定 ABI，一份扩展兼容 Python 3.9+。

## 目录结构

```
native/
├── Cargo.toml          # 工程配置（pyo3 + image + fontdue）
├── src/lib.rs          # render_card / render_text 实现
└── bin/                # 编译产物（插件按平台自动加载）
    ├── win64/zcbot_render.pyd     # Windows x64
    └── linux64/zcbot_render.so    # Linux x64
```

## 函数签名

```python
render_text(text, font_path, width=500, font_size=24, padding=20, options=None) -> bytes
render_card(title, content, font_path, timestamp, width=600, padding=30, options=None) -> bytes
```

## options 参数（与 PIL 回退版完全一致）

颜色格式：`"#RRGGBB"` / `"#RRGGBBAA"` / `[r,g,b]` / `[r,g,b,a]`

| 键 | 说明 | 默认 |
|---|---|---|
| `text_color` | 文字颜色 | `[40,40,60,255]` |
| `title_color` | 卡片标题颜色 | `[20,30,60,255]` |
| `content_color` | 卡片内容颜色 | `[60,60,80,255]` |
| `footer_color` | 页脚颜色 | `[160,160,170,255]` |
| `accent_color` | 标题栏左侧彩条 | `[99,102,241,255]` |
| `bg_color` | 纯色背景（有则覆盖默认渐变） | 无 |
| `bg_gradient` | 垂直渐变 `[顶部色, 底部色]` | 卡片默认 `[[248,250,255],[255,255,245]]` |
| `border_color` | 边框颜色 | 无（不画边框） |
| `border_width` | 边框宽度（px） | `2` |
| `radius` | 圆角半径（px） | `0` |
| `font_size` | render_text 字号 | `24` |
| `title_size` / `content_size` / `footer_size` | 卡片各段字号 | `28` / `20` / `14` |
| `line_height` | 行高（px），0=自动 | 文本 `font_size*1.35`，卡片 `30` |
| `padding` | 内边距 | 文本 `20`，卡片 `30` |
| `align` | 对齐：`"left"` / `"center"` / `"right"` | `"left"` |
| `show_footer` | 是否显示卡片页脚 | `true` |
| `footer_text` | 页脚右侧文字，传 `""` 或 `None` 则不显示 | `"ZGRIC"` |

示例：

```python
render_card(
    "公告", "这是一条测试公告",
    font_path, "2026-08-07 10:00", 600, 30,
    {
        "bg_gradient": [["#1e293b", "#334155"]],
        "title_color": "#ffffff",
        "content_color": "#cbd5e1",
        "accent_color": "#38bdf8",
        "radius": 12,
        "border_color": "#475569",
        "border_width": 1,
        "align": "center",
    },
)
```

## CI 编译（推荐，无需本地工具链）

推送后由 `.github/workflows/build-zcbot-render.yml` 自动构建，也可手动触发：

```bash
# Windows / Linux 各出一个 artifact，内含 zcbot_render.pyd / zcbot_render.so
gh workflow run build-zcbot-render.yml
gh run watch          # 等待完成
gh run download <run_id> --pattern 'zcbot_render-*'
```

下载解压后放入对应平台目录：

- `native/bin/win64/zcbot_render.pyd`（Windows x64）
- `native/bin/linux-x86_64/zcbot_render.so`（Linux x64，aarch64 用户放 `linux-aarch64/`）

## 本地编译（需要 MSVC 工具链 / Linux gcc）

```bash
pip install maturin
cd plugins/image_renderer/native
maturin build --release
# 从 target/wheels/*.whl 中取出 .pyd / .so，重命名为 zcbot_render.pyd / .so
# 放入 bin/win64/ 或 bin/linux64/
```

## 插件回退

`plugins/image_renderer/main.py` 启动时按平台探测 `native/bin/<平台>/` 下的扩展：
找到则使用原生渲染；找不到或加载失败自动回退 PIL（Pillow），功能与参数一致。

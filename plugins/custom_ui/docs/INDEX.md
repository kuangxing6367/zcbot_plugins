# custom_ui 开发文档

`custom_ui`（个性化前端）插件用于**接管 ZCBOT 的 Web 管理面板**：从 GitHub 拉取网页模板（zip），让用户选择 / 下载 / 安装 / 切换模板，把整个后台换成自己的前端。

> 目标读者：想给 ZCBOT 写一套**自定义前端模板**的开发者，以及想二次开发 custom_ui 插件本身的开发者。

---

## 目录

- [一、架构总览](#一架构总览)
- [二、安装与使用](#二安装与使用)
- [三、数据目录结构](#三数据目录结构)
- [四、模板开发指南](#四模板开发指南)
- [五、HTTP API 参考](#五http-api-参考)
- [六、接管机制（框架侧）](#六接管机制框架侧)
- [七、二次开发 custom_ui](#七二次开发-custom_ui)
- [八、常见问题](#八常见问题)

---

## 一、架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                    用户浏览器                                │
│  http://host:8080/  →  框架根路由                            │
└────────────────────────┬────────────────────────────────────┘
                         │ 插件接管后 redirect
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                 /custom_ui/  (模板网页)                      │
│  服务当前激活模板：templates/<active>/index.html 及静态资源    │
│  无激活模板时 redirect 到 /custom_ui/manage                  │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│               /custom_ui/manage  (管理页)                    │
│  模板列表 / 下载 / 切换激活 / 接管前端 / 恢复默认              │
│  manage.html（插件代码目录）                                 │
└─────────────────────────────────────────────────────────────┘
```

**数据流：**
1. 插件启用 → **默认不接管**，框架默认 WebUI 照常显示
2. 用户进入 框架后台 → 插件 WebUI → custom_ui 模板管理页（或直接访问 `/custom_ui/manage`）
3. 管理页从 GitHub（`kuangxing6367/zcbot_plugins/webui/`）列出可用模板 zip
4. 用户点"下载" → 插件从 GitHub raw 拉取 zip → 解压到 `plugins_dat/custom_ui/templates/<name>/`
5. 用户点"切换启用" → 写入 `active.txt`
6. 用户点"接管前端" → 写入 `override.txt` + 调用框架 `ctx.override_webui()` → 根路由 `/` 开始 redirect 到 `/custom_ui/`
7. 刷新网页，模板生效；重启框架后接管状态自动恢复

**权限说明：** 下载 / 切换 / 接管 / 恢复 等写操作 API 均需登录（`Authorization: Bearer <token>` 或 `zcbot_token` cookie）。

---

## 二、安装与使用

### 2.1 安装

- 通过框架后台「插件市场」搜索 `custom_ui` 安装（版本 ≥ 1.1.1）
- 或手动放置到插件目录 `plugins/custom_ui/` 后启用

### 2.2 使用流程

1. 后台「插件管理」确认 `custom_ui` 已启用（`running`）
2. 进入 后台「插件 WebUI」→ 点击「个性化前端」（或浏览器直接访问 `/custom_ui/manage`）
3. 在「可用的远程模板」点 **下载**
4. 在「已下载模板」点 **切换启用**
5. 点 **接管前端**（弹层/按钮）→ 框架根路由 `/` 开始跳转到你的模板
6. 刷新网页，生效

### 2.3 恢复框架默认前端

- 模板管理页点「禁用插件，恢复框架默认前端」
- 或访问 `/reset` 恢复页（登录后恢复）
- 插件被禁用 / 卸载 / 删除时，接管自动回退框架默认 WebUI

---

## 三、数据目录结构

custom_ui 的模板和状态标记都存放在**插件数据目录**（`plugins_dat/custom_ui/`），插件更新 / 重装不会覆盖：

```
plugins_dat/custom_ui/
├── templates/                 # 已下载的模板（每个 zip 解压一个目录）
│   └── <模板名>/
│       ├── index.html         # 模板入口页（必填）
│       ├── css/               # 静态资源（可选）
│       ├── js/                # 静态资源（可选）
│       └── img/               # 静态资源（可选）
├── active.txt                 # 当前激活的模板名（内容为模板名）
└── override.txt               # 接管标记（内容为 "1" 表示接管中）
```

**代码目录**（`plugins/custom_ui/`，更新会被覆盖）：

```
plugins/custom_ui/
├── main.py                    # 插件逻辑
├── plugin.yaml                # 插件元信息 + GitHub 更新源
├── manage.html                # 模板管理页（框架插件 WebUI 进入）
└── web/
    └── index.html             # ctx.webui 入口（跳转到 /custom_ui/manage）
```

> 说明：`active.txt` / `override.txt` 是运行时状态，随插件数据保留；`templates/` 是用户下载的模板，更新插件不会删除。

---

## 四、模板开发指南

模板是一个 **zip 包**，内部结构即一个静态网站。放入插件仓库顶层 `webui/` 目录（如 `webui/<模板名>.zip`）。

### 4.1 模板 zip 结构

```text
<模板名>.zip
├── index.html          # 入口页（必填，根路径访问的文件）
├── css/
│   └── style.css
├── js/
│   └── app.js
└── img/
    └── logo.png
```

- zip 根目录直接放文件（**不要**套一层外层文件夹）
- 入口文件必须是 `index.html`
- 支持任意静态资源（css / js / img / 字体等），引用方式用相对路径或 `/custom_ui/<路径>` 均可

### 4.2 被接管后的 URL 规则

模板接管后，浏览器访问的 URL 是：

| 路径 | 对应文件 |
| ---- | ---- |
| `/custom_ui/` | `templates/<active>/index.html` |
| `/custom_ui/<path>` | `templates/<active>/<path>`（防目录穿越） |

框架静态资源 `/css/*`、`/js/*`、`/img/*` **不会**被模板接管（仍指向框架自身前端），所以模板内资源建议用相对路径。

### 4.3 调用框架 API

模板页面可以直接调用框架后台 API（跨域同源，鉴权同框架）：

```javascript
// 登录后浏览器 localStorage 存有 token
const token = localStorage.getItem('zcbot_token')

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: 'Bearer ' + token } : {}),
    },
  })
  return r.json()
}

// 示例：拉取框架状态
const d = await api('/api/dashboard')
console.log(d.data)
```

可用 API 见框架文档 `docs/API.md`（仪表盘、插件、命令、用户、群组、日志、配置等）。

### 4.4 打包并发布模板

```bash
# 在插件仓库根目录
cd webui
zip -r mytheme.zip mytheme/   # 注意 zip 根目录直接是 index.html，不套 mytheme/ 层
```

或者用 Python：

```python
import zipfile, os
src = 'mytheme'  # 目录内直接是 index.html
with zipfile.ZipFile('webui/mytheme.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(src):
        for f in files:
            fp = os.path.join(root, f)
            z.write(fp, os.path.relpath(fp, src))
```

推送 `webui/<模板名>.zip` 到 `kuangxing6367/zcbot_plugins` 仓库 `main` 分支后，custom_ui 管理页即可看到新模板（自动刷新列表）。

> 模板源仓库与插件源仓库相同：`kuangxing6367/zcbot_plugins`，模板 zip 放在仓库顶层 `webui/` 目录。

---

## 五、HTTP API 参考

所有 API 前缀 `/api/custom_ui/`。除 GET 模板列表外，写操作均需登录。

### 5.1 获取模板列表

`GET /api/custom_ui/templates`

返回：远端可用模板 + 本地已下载 + 当前激活。

```json
{
  "code": 0,
  "data": {
    "remote": [
      { "name": "default", "file": "default.zip", "size": 11025, "download_url": "..." }
    ],
    "local": ["default"],
    "active": "default",
    "error": ""
  }
}
```

### 5.2 下载模板

`POST /api/custom_ui/templates/<name>/download`

从 GitHub 下载 `<name>.zip` 并解压到本地 `templates/<name>/`（已存在则覆盖）。

```json
{ "code": 0, "msg": "模板已下载并安装" }
```

### 5.3 切换激活模板

`POST /api/custom_ui/templates/<name>/activate`

将 `<name>` 设为当前激活模板（刷新网页生效）。需先下载。

### 5.4 接管前端

`POST /api/custom_ui/override`

启用前端接管：写入 `override.txt` + 调用框架 `ctx.override_webui()`，根路由 `/` 开始 redirect 到 `/custom_ui/`。**需已有激活模板**。

### 5.5 取消接管

`DELETE /api/custom_ui/override`

取消接管：清除标记 + 调用框架 `clear_override_webui()`，回到框架默认 WebUI。

### 5.6 恢复框架默认前端（禁用插件）

`POST /api/custom_ui/reset`

卸载 custom_ui 插件并置为停用，回到框架默认 WebUI（接管自动回退）。

### 5.7 页面路由

| 路径 | 说明 |
| ---- | ---- |
| `/custom_ui/` | 当前激活模板入口（无激活则跳 `/custom_ui/manage`） |
| `/custom_ui/<path>` | 当前激活模板的静态资源 |
| `/custom_ui/manage` | 模板管理页 |

---

## 六、接管机制（框架侧）

custom_ui 依赖框架的 `override_webui` 能力。框架侧相关改动：

### 6.1 ctx.override_webui()

插件调用 `ctx.override_webui()` 后，框架记录接管插件名。此时框架根路由 `/` 变为：

```python
entry = framework.plugin_loader.get_override_webui()
if entry:
    return redirect(f'/{entry}/', code=302)
return send_from_directory(_web_root_dir(), 'index.html')
```

即 `/` 302 重定向到 `/custom_ui/`（插件自托管前端）。

### 6.2 自动回退

插件被禁用 / 卸载 / 删除时，框架 loader 自动清除接管标记（`unload_plugin` 中调用 `clear_override_webui`），根路由回到框架默认 WebUI。

### 6.3 刷新过快检测（框架内置）

框架 `before_request` 钩子统计页面导航请求：同一 IP 5 秒内刷新 ≥5 次时，重定向到 `/reset` 恢复页（排除 `/api/` 与静态资源；`/reset` 自身不触发）。

### 6.4 路由注册方式（重要）

插件动态注册 Flask 路由时，不能用 `app.add_url_rule`（Flask 处理首次请求后调用会抛异常）。custom_ui 使用底层方式：

```python
from werkzeug.routing import Rule
if rule_path not in {str(r.rule) for r in app.url_map.iter_rules()}:
    app.url_map.add(Rule(rule_path, endpoint=endpoint, methods=methods))
app.view_functions[endpoint] = fn  # 已存在时仅刷新 view 函数
```

---

## 七、二次开发 custom_ui

### 7.1 插件入口

`register(ctx)` 流程：

```python
def register(ctx):
    _init_dat_dir(ctx)              # 数据目录（plugins_dat/custom_ui）
    ctx.webui("个性化前端", "index.html", icon="🎨", order=60)  # 框架插件 WebUI 入口
    _register_routes(ctx)           # Flask 路由 + API
    if _is_override_enabled():      # 接管状态恢复（重启保持）
        active = _get_active_template()
        if active:
            ctx.override_webui()
```

### 7.2 关键内部函数

| 函数 | 作用 |
| ---- | ---- |
| `_dat_dir()` | 插件数据目录（模板 + 状态标记） |
| `_templates_root()` | 模板根目录 |
| `_get_active_template()` / `_set_active_template(name)` | 读取 / 写入激活模板 |
| `_is_override_enabled()` / `_set_override(bool)` | 读取 / 写入接管标记 |
| `_list_remote_templates()` | GitHub API 列出仓库 `webui/` 下的模板 |
| `_download_template(ctx, name)` | 下载 zip 并解压到 `templates/<name>/` |
| `_safe_template_name(name)` | 模板名白名单校验（字母数字 `_-.`） |
| `_github_raw_candidates(path)` | 生成 GitHub raw 候选地址（代理 → 镜像 → 直连） |

### 7.3 目录 / 文件约定

- 模板 zip 放插件仓库顶层 `webui/<name>.zip`
- 管理页 `manage.html` 放插件代码根目录
- 状态标记（`active.txt` / `override.txt`）与模板存插件数据目录

### 7.4 修改管理页

编辑 `manage.html`（原生 HTML/JS，无构建步骤）。管理页调用的 API 见[第五节](#五http-api-参考)。改完推送到插件仓库，服务器插件管理页点"更新"即可生效（新版本需 `plugin.yaml` 版本号递增）。

---

## 八、常见问题

**Q: 插件启用后访问 `/` 还是框架默认 WebUI？**
A: 正常。custom_ui 默认**不接管**，需先在模板管理页下载 / 切换模板，再点"接管前端"。

**Q: 模板管理页显示"未登录"？**
A: 管理页的写操作 API 需要登录。请先在框架后台登录，或在模板页通过 `/api/login` 登录（token 存 `localStorage.zcbot_token`）。

**Q: 点"接管前端"报"请先下载并切换启用一个模板"？**
A: 需要先下载模板（`POST /api/custom_ui/templates/<name>/download`）并激活（`activate`），然后才能接管。

**Q: 接管后想改回默认前端？**
A: 管理页点「禁用插件」或调用 `POST /api/custom_ui/reset`；插件卸载也会自动回退。

**Q: 刷新网页被带到 /reset？**
A: 框架检测到同一 IP 5 秒内刷新 ≥5 次会引导到 `/reset` 恢复页。这是框架的防抖机制，正常刷新不会触发。

**Q: 怎么新增一个模板？**
A: 做 zip（根目录直接是 index.html）→ 推送到 `kuangxing6367/zcbot_plugins` 仓库 `webui/` → 管理页刷新即可见。

**Q: 模板里的接口请求跨域 / 401？**
A: 同源（同框架端口）不会跨域；401 说明未带 token，参考 [4.3 调用框架 API](#43-调用框架-api)。

---

## 相关文档

- [框架插件开发详解](https://github.com/kuangxing6367/zcbot/blob/main/docs/plugin-tutorial.md) — 插件基础开发
- [框架 API 参考](https://github.com/kuangxing6367/zcbot/blob/main/docs/api-reference.md) — `ctx` 全部方法
- [框架 Web API](https://github.com/kuangxing6367/zcbot/blob/main/docs/API.md) — 后台 HTTP API（模板可调用）

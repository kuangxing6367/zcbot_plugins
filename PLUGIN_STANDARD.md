# ZCBOT 插件开发标准规范 (zcbot standard)

> 本文档定义 ZCBOT（zgric_onebot11）官方插件源中插件的统一结构与 API 约定。
> 所有插件须遵循本规范，以便被框架的插件加载器（`framework/loader.py`）正确识别、配置与热加载。
>
> 适用场景：将其他框架（如 AstrBot）的插件**按功能逻辑**迁移到本框架时，
> 仅参考原插件"做什么"，不照搬原代码语法结构。

---

## 1. 目录结构

每个插件是 `plugins/<plugin_id>/` 下的一个独立目录：

```
plugins/<plugin_id>/
├── main.py            # 必备：插件入口（含 __plugin_meta__ 与 register(ctx)）
├── plugin.yaml        # 必备：元信息 + 依赖声明
├── _conf_schema.json  # 可选：配置项 schema（Web UI 配置页渲染，含 description 注释）
├── requirements.txt   # 可选：Python 依赖（同 plugin.yaml 的 dependencies，二选一）
├── web/               # 可选：插件内嵌 WebUI（HTML/JS/CSS），由 ctx.webui() 注册
├── <其他 .py>         # 可选：拆分的逻辑模块
└── README.md          # 可选：插件说明
```

- `<plugin_id>`：全小写、下划线分隔（如 `send_like`、`message_guard`、`qqadmin`）。
- 插件**数据/缓存目录**不要写在代码目录里，用 `ctx.get_data_dir()` 获取
  `plugins_dat/<plugin_id>/`（框架自动创建）。

---

## 2. 插件入口 `main.py`

```python
"""插件简述（一句话说明功能 + 迁移来源）"""
__plugin_meta__ = {
    "name": "插件中文名",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "一句话功能描述",
    "priority": 50,          # 数字越小越先执行；守卫类用 2~10，普通命令用 50
}

def register(ctx):
    """框架加载插件时调用，在此注册命令/事件/任务"""
    ctx.command("/cmd", handle_cmd, priority=50,
                alias="/别名", description="命令说明")
    ctx.on_raw_message(on_raw)          # 原始消息接管点
    ctx.task("*/10 * * * *", cleanup)   # 定时任务（cron）
```

### 2.1 `__plugin_meta__` 字段

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| `name` | str | 展示名（中文） |
| `version` | str | 语义化版本 |
| `author` | str | 作者 |
| `desc` | str | 功能描述（列表页展示） |
| `priority` | int | 插件优先级（决定 `on_raw_message` 触发顺序与命令匹配先后） |

### 2.2 `register(ctx)` 注册方式

| 方法 | 签名 | 用途 |
| ---- | ---- | ---- |
| `ctx.command` | `(pattern, handler, priority=50, alias=None, description=None, require_admin=False, require_superuser=False)` | 注册命令 |
| `ctx.on_raw_message` | `(handler)` | 注册原始消息处理器（返回 `True` 接管并阻断后续） |
| `ctx.task` | `(cron_expr, executor, description=None)` | 注册定时任务 |
| `ctx.on` / `ctx.emit` / `ctx.aemit` | 事件订阅/发布 | 系统事件总线 |

- `pattern` 为正则或命令名；命令 handler 形如 `def handle(event, match)`，
  `match.group(1)` 为命令后参数文本，`event.message` 为完整消息文本，
  `event.user_id` / `event.group_id` / `event.is_group` 可用。
- `require_admin=True` → 需群管/群主/超管；`require_superuser=True` → 需超管（优先级更高）。

### 2.3 原始消息接管 `on_raw_message`

```python
async def on_raw(raw: dict, bot_name: str) -> bool:
    # raw 为 OneBot 11 原始事件（message 可能是消息段数组）
    # 返回 True  → 框架跳过该消息的后续全部处理（被本插件接管）
    # 返回 False/None → 消息继续走正常流程
    return False
```

---

## 3. `ctx` 上下文 API（plugins 可用能力）

| 类别 | 方法 | 说明 |
| ---- | ---- | ---- |
| 配置 | `get_config(key, default)` / `get_all_config()` | 读取 Web UI 配置（带 TTL 缓存） |
| 发送 | `send_msg(user_id, group_id, message, auto_escape=False, bot=None)` | 同步发送（自动判断私/群） |
| 发送 | `asend_msg(...)` | 异步发送（推荐 async handler 使用） |
| OneBot | `api(action, bot=None, **params)` / `aapi(...)` | 调用任意 OneBot 11 API（同步/异步） |
| OneBot | `onebot.<method>(...)` | 38 个 OneBot 方法的封装 |
| 群管 | `ban/kick/mute_all/set_card/get_member_list/get_member_info`（含 `a*` 异步版） | 群管理快捷方法 |
| 权限 | `is_group_admin/is_group_owner/is_superuser/is_blacklisted/get_user_role` | 身份判断 |
| 群开关 | `is_plugin_enabled_in_group/enable/disable_plugin_in_group` | 群级插件开关 |
| 数据库 | `db_query/db_query_one/db_execute/db_insert/db_execute_many/create_table`（+ `_async` 异步版） | SQL 操作（连接池，自动适配方言） |
| 数据库 | `db_connection()` | 获取连接（事务/多语句） |
| 日志 | `log(msg, level='info')` / `ctx.logger.info(...)` | 插件日志 |
| 异步 | `run_async(func, *args)` | 线程池执行耗时操作（图片渲染/网络） |
| 审计 | `audit_log(action, target_type, target_name, detail, result, error_message)` | 写入审计日志 |
| 仪表盘 | `dashboard_card(title, handler, icon, priority)` | 注册仪表盘卡片 |
| WebUI | `webui(title, entry='index.html', icon, order)` / `override_webui()` | 内嵌/接管前端 |
| 扩展 | `register_group_extension(key, title, handler, ext_type)` / `register_user_extension(...)` | WebUI 群/用户管理页扩展 |
| 数据 | `get_data_dir()` | 插件数据目录 |

### 3.1 发送富媒体（图片/文件）

通过 OneBot 消息段（CQ 码或字典）拼接进 `message`：

```python
# 本地图片
ctx.send_msg(group_id=gid, message="[CQ:image,file=file:///abs/path.png]")
# 网络图片
ctx.send_msg(group_id=gid, message="[CQ:image,file=https://example.com/a.png]")
# 文本 + 图片
ctx.send_msg(user_id=uid, message=["你好", {"type":"image","data":{"file":"file:///abs/a.png"}}])
```

### 3.2 数据库

- 表名/列名用反引号或参数化；值一律用 `%s` 参数化（框架自动适配 MySQL/SQLite 方言）。
- 建表用 `ctx.create_table(ddl)`，无需关心方言差异。
- 用户/群基础表已存在：`users(user_id, role, is_blacklist, ...)`、
  `group_members(group_id, user_id, role, ...)`、`groups(group_id, ...)`。

### 3.3 异步模型

- 命令 handler 可为普通 `def` 或 `async def`。
- `async def` 中优先使用 `aapi` / `asend_msg` / `db_*_async`，避免阻塞事件循环。
- 图片渲染等 CPU 密集操作用 `ctx.run_async(...)` 丢进线程池。

---

## 4. 配置：`plugin.yaml` + `_conf_schema.json`

### 4.1 `plugin.yaml`

```yaml
name: 插件中文名
version: 1.0.0
author: ZGRIC
description: 一句话描述
priority: 50

dependencies:
  python:
    - "Pillow>=10.0.0"
    - "httpx"
```

- `dependencies.python` 中的包会在框架启动时自动检查并安装缺失项。

### 4.2 `_conf_schema.json`

键为配置项名；每个键含 `description`（**配置项注释，Web UI 详情页会显示**）、
`type`（`string`/`number`/`boolean`/`array`）、`default`、可选 `hint`：

```json
{
  "rate_enable": {
    "description": "限流开关",
    "type": "boolean",
    "default": false
  },
  "rate_count": {
    "description": "窗口内允许的消息数",
    "type": "number",
    "default": 5
  }
}
```

> ⚠️ 规范要点：配置项的**注释必须写在 `_conf_schema.json` 的 `description` 字段**，
> 后端 `get_plugin_config_schema` 会原样返回给前端详情页渲染（参考插件详情页 bug 修复：
> 详情弹窗须消费 `_conf_schema.json` 的 `description`，而非 `plugin.yaml` 的 `config` 段）。

---

## 5. 迁移约定（从其他框架迁来）

1. **只搬功能逻辑**：理解原插件"提供哪些命令 / 触发什么条件 / 调用什么外部接口 / 存什么数据"，
   用本规范的 API 重新实现；不复制原代码的函数结构、变量命名与语法风格。
2. **命令设计**：保留可识别的中文/英文命令名与别名，便于老用户迁移；权限要求用
   `require_admin` / `require_superuser` 表达。
3. **外部接口**：原插件依赖的第三方 API（天气、音乐、B站、列车等）端点若可公开获取，
   在新插件中按相同功能重新封装；不复制原作者的私有密钥/签名逻辑。
4. **数据兼容**：如需读取原插件遗留数据，自建映射表或导入脚本，不在新插件里耦合旧表结构。
5. **配置项**：将旧配置项映射到新的 `_conf_schema.json`，保证 `description` 注释完整。

---

## 6. 最小可运行示例（echo）

见 `plugins/echo/main.py`：注册 `/echo` 命令，回显参数或返回 `PONG`。

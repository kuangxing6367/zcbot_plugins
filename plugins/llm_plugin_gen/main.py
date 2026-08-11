"""
LLM 开发助手插件
================================
通过 OpenAI 兼容接口让 LLM 编写插件、管理插件文件，支持自定义人格与 skills。

核心能力：
  1. /ai new [标题]        — 创建新会话（自动分配两位编号 #00~#99，自动进入）
  2. /ai <需求>            — 当前会话发起请求（无会话则自动创建）
  3. /ai #编号 <需求>      — 指定会话发起请求
  4. /ai #编号 N           — 回复 AI 提问，选择第 N 个选项
  5. /ai #编号 say <文字>  — 回复 AI 提问，自由补充说明
  6. /ai continue 修改需求：xxx — 中途纠错（基于当前进度修改后续步骤）
  7. /ai list              — 列出所有会话
  8. /ai set #编号         — 进入指定会话
  9. /ai del [#编号]       — 删除会话（默认当前）
  10. /ai stop             — 暂停当前正在运行的会话（也可 /ai #编号 stop）
  11. /pluginlist          — 查看已加载插件

新增 AI 能力（直接对 AI 说即可）：
  - 「检查框架更新」→ 调用 check_framework_update（对比 GitHub Release 与本地 VERSION）
  - 「更新框架」→ 调用 update_framework（下载最新代码→自动备份到 data/backups/→只覆盖白名单文件，需重启生效）

工作流（AI 项目助手规范 V1.0，四阶段闭环）：
- 阶段一 启动：后台深度思考，只提 1~3 个口语化疑点（ask_user 提交）
- 阶段二 规划：拆解为恰好 6 项待办清单（每项 ≤10 字），用户确认后才动工
- 阶段三 执行：逐项完成并广播进度（✅ 已完成：【N. 名称】）
- 阶段四 干预：/ai stop 制动；/ai continue 修改需求：xxx 纠错；完成后可在原会话指出错误要求修正

会话机制：
- 每个会话分配两位编号（#00~#99），编号用尽（00~99 全占用）时自动抛弃最旧会话
- 每轮请求 AI 必须先解释理解与计划，用 ask_user 提交，用户批准后才继续修改文件
- 上下文超限自动压缩（旧消息 LLM 摘要 + 保留最近消息），函数调用不限制轮数

配置项（_conf_schema.json，Web UI 可改）：
  base_url / api_key / model / temperature / max_tokens
  max_tool_rounds / session_max_msgs / session_max_chars
  persona / skills / cwd

权限说明：命令仅超管可用；文件操作不受路径限制（超管自负其责）。
"""
import ast
import asyncio
import fnmatch
import json
import os
import re
import time
import traceback
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, jsonify, request

# ========== 第三方库懒加载说明 ==========
# httpx 为可选依赖（仅在发起 LLM 请求时按需 import），见 _chat_once

__plugin_meta__ = {
    "name": "LLM 开发助手",
    "version": "1.7.0",
    "author": "ZGRIC",
    "desc": "对话式 LLM 插件开发（AI 项目助手工作流 V1.0：澄清疑点→6 项待办确认→逐项广播→stop/continue 干预，全异步；支持检查/更新框架源码）",
    "priority": 100,
}

# 默认人格
_DEFAULT_PERSONA = "你是 ZCBOT OneBot QQ 机器人框架的插件开发专家代理（AI 项目助手），擅长把需求变成可运行、健壮的 Python 插件代码，严格遵循 AI 项目助手工作流规范 V1.0（四阶段闭环）。"

# 工作流提示（AI 项目助手工作流规范 V1.0：四阶段闭环）
_AGENT_WORKFLOW = """\
## 工作流规范（V1.0）：四阶段闭环

### 阶段一 · 启动：需求确认与疑点澄清
- 收到 /ai <需求> 后，先在后台深度思考，但**绝不输出思考过程和推导步骤**
- 只针对需求中不确定的地方，用简单、口语化的方式一次性提出 1~3 个疑点（无疑点则跳过，直接进入规划）
- 示例：需求"写个插件" → ask_user 提问："收到！这个插件是要在 WebUI 展示，还是纯 QQ 指令就行呀？"
- 所有提问/确认一律通过 ask_user 提交；**不要先输出文本再调用 ask_user**，避免重复播报

### 阶段二 · 规划：6 项待办清单
- 需求确认后，把大任务拆解为**恰好 6 个待办事项**（6 步闭环；极简需求可少于 6 项但须说明原因）
- 每个待办项的中文描述**不超过 10 个字**，极简、一目了然
- 通过 ask_user 提交清单，格式如下：
  任务已拆解，请确认以下待办事项：
  > 1. [ ] 确定接口方案
  > 2. [ ] 编写插件主逻辑
  > 3. [ ] 设计配置结构
  > 4. [ ] 开发 WebUI 页面
  > 5. [ ] 加载与自测
  > 6. [ ] 输出最终代码
- **中断机制：用户确认后你才开始正式动工**（确认前禁止 write/edit/rm/append/rename/加载插件）

### 阶段三 · 执行：单步完成广播
- 按顺序逐个完成待办项；**每完成 1 个待办项，立即用普通文本广播进度**（不要用 ask_user）：
  ✅ 已完成：【2. 编写插件主逻辑】

### 阶段四 · 干预：紧急制动与纠正
- 停止：用户随时发 /ai stop 或 /ai #编号 stop 终止当前会话，收到后立即停下并汇报已完成步骤
- 非破坏性纠错：用户发 /ai continue 修改需求：xxxx，基于当前进度修改后续步骤，不要推翻已完成内容
- 后置修改：全部步骤完成并输出结果后，用户仍可在原会话中指出错误 → 重新修正并更新结果

## 通用规则
- 用户回复方式：`/ai #编号 序号`（选择选项）或 `/ai #编号 say 补充内容`
- 函数调用不限制轮数；每个执行节点都要用普通文本向用户说明进度
- 动手前说明可能的原因与风险；失败时说明失败原因与修复思路
- 简单直接的需求不要过度思考、不要长篇分析，尽快执行
- 任务完成总结格式：
  ✅ 完成：<做了什么>
  📁 文件：<涉及的路径列表>
  🔍 验证：<load_plugin 结果等>
- 过程中如需要用户决策（路径、风格、功能取舍），随时 ask_user

## 工具使用规范
- 先探查再动手：动手前至少 ls 一次相关目录，读文档索引与现有代码
- 小步修改：新文件用 write；改已有文件用 edit 精确替换，不要整文件重写
- 文件写完用 read 复查关键片段
- 不确定路径/内容时用 search 确认，不要臆测
- 删除/移动前先 ls 确认目标
"""

# 插件开发提示（精简；完整文档索引在 docs/INDEX.md，需要时用 ls/read 读取）
_PLUGIN_DEV_GUIDE = """\
## 插件开发提示
- 完整开发文档索引：`plugins/llm_plugin_gen/docs/INDEX.md`，编写插件前先读索引，再按需 ls/read 对应文档（省 token）
- 写完代码后务必调用 load_plugin 加载；失败则 read/edit/reload_plugin 修复
"""

# 默认 skills（可从配置覆盖）
_DEFAULT_SKILLS = [
    "生成插件：根据用户需求生成符合框架规范的插件代码",
    "修改插件：阅读现有插件代码后修复 Bug / 增加功能",
    "文件操作：使用 ls/read/search/write/edit/append/mkdir/rm/rename 管理插件目录文件",
    "插件管理：load_plugin / unload_plugin / reload_plugin / list_plugins",
]


# 函数调用工具定义
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ls",
            "description": "列出指定路径下的文件和目录（支持通配符，如 plugins/*.py）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录或文件路径，相对 cwd 或绝对路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "写入/新建文件（覆盖原内容）。用于创建新插件文件或重写文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径，如 plugins/my_plugin/main.py"},
                    "content": {"type": "string", "description": "文件完整内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "修改已有文件：把 old_text 替换为 new_text（首次出现处）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "old_text": {"type": "string", "description": "要被替换的原文（须精确匹配）"},
                    "new_text": {"type": "string", "description": "替换后的新文本"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rm",
            "description": "删除文件或目录（目录递归删除）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的文件/目录路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "读取文件内容（带行号，可指定起始行/行数）。比 ls 更适合看大文件，如 plugins/xxx/main.py",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "start": {"type": "integer", "description": "起始行（从 1 开始，默认 1）"},
                    "limit": {"type": "integer", "description": "读取行数（默认 200）"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "在目录或文件中搜索文本（正则），返回匹配的文件与行。用于确认某符号/函数/文案是否存在",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "搜索的目录或文件路径"},
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "glob": {"type": "string", "description": "文件过滤，如 *.py"},
                },
                "required": ["path", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mkdir",
            "description": "递归创建目录（已存在不报错）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要创建的目录路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append",
            "description": "向文件末尾追加内容（文件不存在则创建）。用于给列表/配置加项，或分步构建大文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "要追加的内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename",
            "description": "重命名/移动文件或目录",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "原路径"},
                    "new_path": {"type": "string", "description": "新路径"},
                },
                "required": ["path", "new_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "向用户提问/确认：提交你的理解与计划，等待用户批准后再继续。options 最多 4 个；用户回复 /ai #编号 序号 或 /ai #编号 say 内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "要问用户的问题（含你的理解与计划）"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "候选选项（可选，最多 4 个）",
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_plugin",
            "description": "加载插件：把 plugins/<插件名> 目录下的插件加载到框架并注册命令。写插件完成后必须调用此工具使其生效",
            "parameters": {
                "type": "object",
                "properties": {
                    "plugin_name": {"type": "string", "description": "插件目录名（与 main.py 所在目录一致）"},
                },
                "required": ["plugin_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unload_plugin",
            "description": "卸载插件：从框架移除并清理其命令/任务",
            "parameters": {
                "type": "object",
                "properties": {
                    "plugin_name": {"type": "string", "description": "插件目录名"},
                },
                "required": ["plugin_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reload_plugin",
            "description": "重载插件：先卸载再重新加载（修改代码后热更新用）",
            "parameters": {
                "type": "object",
                "properties": {
                    "plugin_name": {"type": "string", "description": "插件目录名"},
                },
                "required": ["plugin_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_plugins",
            "description": "列出当前已加载的插件及版本",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_framework_update",
            "description": "检查框架是否有新版本（对比 GitHub Release 与本地 VERSION，返回本地/最新版本与是否有更新）",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_framework",
            "description": "从 GitHub 更新框架源码：下载最新代码→自动备份旧 framework 到 data/backups/→只覆盖 framework/web/sql/main.py 等白名单文件（保留 plugins/、data/、config.yaml），更新后需重启生效。确认执行时请传 confirm='yes'",
            "parameters": {
                "type": "object",
                "properties": {
                    "confirm": {"type": "string", "description": "确认标记，传 'yes' 才执行更新，否则只返回说明"},
                },
                "required": ["confirm"],
            },
        },
    },
]


_USAGE_TEXT = """\
用法:
/ai new [标题]          创建新会话（自动进入，分配 #00~#99）
/ai <需求>              当前会话发起请求（无会话自动创建）
/ai #编号 <需求>        指定会话发起请求
/ai #编号 1~4           回复 AI 提问（选择选项）
/ai #编号 say 补充      回复 AI 提问（自由补充说明）
/ai continue 修改需求：xxx  中途纠错（基于当前进度修改后续步骤）
/ai list                列出所有会话
/ai set #编号           进入指定会话
/ai del [#编号]         删除会话（默认删除当前）
/ai stop                暂停当前正在运行的会话（也可 /ai #编号 stop）
/pluginlist             查看已加载插件
"""


def register(ctx):
    """插件注册入口"""
    # _init_db 为 async：在事件循环内则调度任务，否则同步跑完（CLI/同步加载场景）
    try:
        asyncio.get_running_loop().create_task(_init_db(ctx))
    except RuntimeError:
        asyncio.run(_init_db(ctx))
    ctx.command(
        "/ai", handle_ai,
        priority=100,
        alias=["/ai开发", "/生成插件", "/写插件", "/开发"],
        require_superuser=True,
        description="与 LLM 对话，可让 AI 编写/修改/加载插件（群内超管可用），用法: /ai <需求>",
    )
    ctx.command(
        "/pluginlist", handle_list_plugins,
        priority=100,
        alias=["/插件列表"],
        require_superuser=True,
        description="列出当前已加载的插件",
    )

    # WebUI 独立页面：AI 对话控制台（网页端以超管 QQ 身份操作，与 QQ 群数据互通）
    ctx.webui("AI 助手", "index.html", icon="🤖", order=5)
    _register_webui_routes(ctx)


# ---------------------------------------------------------------- 配置

def _get_config(ctx, key, default=None):
    return ctx.get_config(key, default)


def _build_system_prompt(ctx, code=None) -> str:
    """组装 system prompt：人格 + 技能 + 插件规范 + 工作流 + 会话编号"""
    persona = str(_get_config(ctx, "persona", "")).strip() or _DEFAULT_PERSONA
    skills = _get_config(ctx, "skills", None)
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.replace('\n', ',').split(',') if s.strip()]
    elif not isinstance(skills, list):
        skills = _DEFAULT_SKILLS

    parts = [persona]
    if skills:
        parts.append("## 技能\n" + "\n".join(f"- {s}" for s in skills))
    parts.append(_PLUGIN_DEV_GUIDE)
    if code:
        parts.append(
            f"## 当前会话\n"
            f"你的当前会话编号是 #{code}。用户会用 `/ai #{code} 序号` 回复你的选项，"
            f"或用 `/ai #{code} say 补充内容`。所有提问都在这条会话内进行。"
        )
    parts.append(_AGENT_WORKFLOW)
    return "\n\n".join(parts)


def _get_cwd(ctx) -> str:
    """LLM 文件操作的基准目录（默认项目根）"""
    cwd = str(_get_config(ctx, "cwd", "")).strip()
    if cwd:
        return os.path.abspath(cwd)
    # 默认：plugins 目录的父目录 = 项目根
    try:
        plugins_dir = ctx._framework.plugin_loader.plugins_dir
        return os.path.dirname(os.path.abspath(plugins_dir))
    except Exception:
        # 回退：本文件所在位置的第三级上级（plugins/<name>/main.py → 项目根）
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------- 运行时状态（内存）

# 正在运行的 AI 任务: (user_id, code) -> asyncio.Task
_active_tasks = {}
# 等待用户回复的上下文: (user_id, code) -> {"msgs": [...], "options": [...]}
_pending_ask = {}


# ---------------------------------------------------------------- 会话与消息（数据库存储，异步）

_SESSION_MAX_MSGS = 30  # 每个会话最多保留的消息条数（可配置 session_max_msgs）
_SESSION_MAX_CHARS = 8000  # 触发自动压缩的总字符阈值（可配置 session_max_chars）


async def _init_db(ctx) -> None:
    """初始化会话/消息/用量表（MySQL 方言，SQLite 模式由框架自动翻译）"""
    try:
        await ctx.db_execute_async(
            "CREATE TABLE IF NOT EXISTS llm_dev_conversations ("
            "id INT PRIMARY KEY AUTO_INCREMENT, "
            "user_id VARCHAR(32) NOT NULL, "
            "code VARCHAR(8) NOT NULL, "
            "title VARCHAR(200), "
            "status VARCHAR(16) DEFAULT 'idle', "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "last_active_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        await ctx.db_execute_async(
            "CREATE TABLE IF NOT EXISTS llm_dev_messages ("
            "id INT PRIMARY KEY AUTO_INCREMENT, "
            "user_id VARCHAR(32) NOT NULL, "
            "code VARCHAR(8) NOT NULL, "
            "role VARCHAR(16) NOT NULL, "
            "content TEXT, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        await ctx.db_execute_async(
            "CREATE TABLE IF NOT EXISTS llm_dev_state ("
            "user_id VARCHAR(32) PRIMARY KEY, "
            "current_code VARCHAR(8), "
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        await ctx.db_execute_async(
            "CREATE TABLE IF NOT EXISTS llm_dev_usage ("
            "id INT PRIMARY KEY AUTO_INCREMENT, "
            "user_id VARCHAR(32) NOT NULL, "
            "prompt_tokens INT DEFAULT 0, "
            "completion_tokens INT DEFAULT 0, "
            "total_tokens INT DEFAULT 0, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        # WebUI 实时进度表（网页轮询展示 AI 每轮说明，读取后清理）
        await ctx.db_execute_async(
            "CREATE TABLE IF NOT EXISTS llm_webui_progress ("
            "id INT PRIMARY KEY AUTO_INCREMENT, "
            "user_id VARCHAR(32) NOT NULL, "
            "code VARCHAR(8) NOT NULL, "
            "text TEXT, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        # 索引（MySQL 方言；SQLite 模式由框架自动翻译）。
        # 注意：MySQL 不支持 CREATE INDEX IF NOT EXISTS，重复创建会报错，
        # 因此逐个 try 忽略"已存在"错误，保证 register 重复执行时幂等。
        for _idx_sql in (
            "CREATE INDEX idx_llm_dev_conv_user ON llm_dev_conversations(user_id, id)",
            "CREATE INDEX idx_llm_dev_msg_user ON llm_dev_messages(user_id, code, id)",
            "CREATE INDEX idx_llm_dev_usage_user ON llm_dev_usage(user_id, id)",
        ):
            try:
                await ctx.db_execute_async(_idx_sql)
            except Exception:
                pass  # 索引已存在，忽略
    except Exception as e:
        ctx.log(f"初始化数据库表失败: {e}", level="error")


async def _get_current_code(ctx, user_id):
    """当前会话编号（可能已失效）"""
    try:
        rows = await ctx.db_query_async(
            "SELECT current_code FROM llm_dev_state WHERE user_id = %s", (str(user_id),))
        if rows and rows[0].get('current_code'):
            return str(rows[0]['current_code'])
    except Exception as e:
        ctx.log(f"读取当前会话失败: {e}", level="warning")
    return None


async def _set_current(ctx, user_id, code):
    """设置当前会话（先删后插，兼容 SQLite/MySQL）"""
    try:
        await ctx.db_execute_async("DELETE FROM llm_dev_state WHERE user_id = %s", (str(user_id),))
        await ctx.db_execute_async(
            "INSERT INTO llm_dev_state (user_id, current_code) VALUES (%s, %s)",
            (str(user_id), code))
    except Exception as e:
        ctx.log(f"写入当前会话失败: {e}", level="warning")


async def _get_conversation(ctx, user_id, code):
    try:
        rows = await ctx.db_query_async(
            "SELECT code, title, status, created_at FROM llm_dev_conversations "
            "WHERE user_id = %s AND code = %s",
            (str(user_id), str(code)))
        return rows[0] if rows else None
    except Exception as e:
        ctx.log(f"查询会话失败: {e}", level="warning")
        return None


async def _allocate_code(ctx, user_id) -> str:
    """分配两位编号（00~99）：优先空位；全占用则抛弃最旧会话复用其编号"""
    try:
        rows = await ctx.db_query_async(
            "SELECT code FROM llm_dev_conversations WHERE user_id = %s", (str(user_id),))
        used = {str(r.get('code')) for r in rows}
        for i in range(100):
            c = f"{i:02d}"
            if c not in used:
                return c
        # 编号用尽：删除最旧会话（last_active_at 最早）
        old = await ctx.db_query_async(
            "SELECT code FROM llm_dev_conversations WHERE user_id = %s "
            "ORDER BY last_active_at ASC, id ASC LIMIT 1",
            (str(user_id),))
        if old:
            await _delete_conversation(ctx, user_id, str(old[0]['code']))
            return str(old[0]['code'])
    except Exception as e:
        ctx.log(f"分配会话编号失败: {e}", level="warning")
    return "00"


async def _create_conversation(ctx, user_id, title=None) -> str:
    """创建新会话并设为当前，返回编号"""
    code = await _allocate_code(ctx, user_id)
    await ctx.db_execute_async(
        "INSERT INTO llm_dev_conversations (user_id, code, title, status) VALUES (%s, %s, %s, 'idle')",
        (str(user_id), code, (title or "").strip()[:200]))
    await _set_current(ctx, user_id, code)
    return code


async def _update_activity(ctx, user_id, code):
    """更新会话活跃时间"""
    try:
        await ctx.db_execute_async(
            "UPDATE llm_dev_conversations SET last_active_at = CURRENT_TIMESTAMP, status = 'running' "
            "WHERE user_id = %s AND code = %s",
            (str(user_id), str(code)))
    except Exception:
        pass


async def _set_status(ctx, user_id, code, status):
    try:
        await ctx.db_execute_async(
            "UPDATE llm_dev_conversations SET status = %s, "
            "last_active_at = CURRENT_TIMESTAMP WHERE user_id = %s AND code = %s",
            (status, str(user_id), str(code)))
    except Exception:
        pass


async def _delete_conversation(ctx, user_id, code):
    """删除会话（含消息、等待上下文、运行任务）"""
    try:
        await ctx.db_execute_async(
            "DELETE FROM llm_dev_conversations WHERE user_id = %s AND code = %s",
            (str(user_id), str(code)))
        await ctx.db_execute_async(
            "DELETE FROM llm_dev_messages WHERE user_id = %s AND code = %s",
            (str(user_id), str(code)))
    except Exception as e:
        ctx.log(f"删除会话失败: {e}", level="warning")
    key = (str(user_id), str(code))
    _pending_ask.pop(key, None)
    task = _active_tasks.get(key)
    if task and not task.done():
        task.cancel()


async def _get_session(ctx, user_id, code) -> list:
    """从数据库读取指定会话历史（不含 system 首条）"""
    try:
        limit = int(_get_config(ctx, "session_max_msgs", _SESSION_MAX_MSGS) or _SESSION_MAX_MSGS)
        rows = await ctx.db_query_async(
            "SELECT role, content FROM llm_dev_messages WHERE user_id = %s AND code = %s "
            "ORDER BY id DESC LIMIT %s",
            (str(user_id), str(code), limit),
        )
        return list(reversed(rows))
    except Exception as e:
        ctx.log(f"读取会话失败: {e}", level="warning")
        return []


async def _append_session(ctx, user_id, code, role, content):
    """写入一条会话消息（按编号隔离，超限裁剪）"""
    try:
        limit = int(_get_config(ctx, "session_max_msgs", _SESSION_MAX_MSGS) or _SESSION_MAX_MSGS)
        char_limit = int(_get_config(ctx, "session_max_chars", _SESSION_MAX_CHARS) or _SESSION_MAX_CHARS)
        await ctx.db_execute_async(
            "INSERT INTO llm_dev_messages (user_id, code, role, content) VALUES (%s, %s, %s, %s)",
            (str(user_id), str(code), role, _clip(content, char_limit)),
        )
        cnt = await ctx.db_query_async(
            "SELECT COUNT(*) AS c FROM llm_dev_messages WHERE user_id = %s AND code = %s",
            (str(user_id), str(code)),
        )
        if cnt and cnt[0].get('c', 0) > limit:
            extra = cnt[0]['c'] - limit
            await ctx.db_execute_async(
                "DELETE FROM llm_dev_messages WHERE user_id = %s AND code = %s "
                "AND id IN (SELECT id FROM (SELECT id FROM llm_dev_messages "
                "WHERE user_id = %s AND code = %s ORDER BY id ASC LIMIT %s) t)",
                (str(user_id), str(code), str(user_id), str(code), extra),
            )
    except Exception as e:
        ctx.log(f"写入会话失败: {e}", level="warning")


async def _maybe_compress(ctx, user_id, code):
    """
    上下文自动压缩：消息条数或总字符超限时，
    把最旧的一半消息交给 LLM 生成摘要，替换为一条 system 摘要，保留最近消息。
    """
    try:
        limit = int(_get_config(ctx, "session_max_msgs", _SESSION_MAX_MSGS) or _SESSION_MAX_MSGS)
        char_limit = int(_get_config(ctx, "session_max_chars", _SESSION_MAX_CHARS) or _SESSION_MAX_CHARS)
        rows = await ctx.db_query_async(
            "SELECT role, content FROM llm_dev_messages WHERE user_id = %s AND code = %s ORDER BY id ASC",
            (str(user_id), str(code)),
        )
        if not rows:
            return
        total = sum(len(str(r.get('content') or '')) for r in rows)
        # 触发阈值：条数超 2 倍上限 或 字符超 2 倍阈值
        if len(rows) <= limit * 2 and total <= char_limit * 2:
            return
        keep_n = max(5, limit // 2)
        old = rows[:-keep_n]
        keep = rows[-keep_n:]
        if not old:
            return

        summary = ""
        try:
            msgs = [
                {"role": "system", "content": "你是上下文压缩器。把下面的对话历史压缩成 300 字以内的中文摘要，"
                                              "保留：用户需求、已完成的文件操作、未完成事项、失败与修复、用户偏好。不要遗漏关键决策。"}
            ]
            for r in old:
                msgs.append({"role": str(r.get('role') or 'user'),
                             "content": str(r.get('content') or '')})
            resp = await _chat_once(ctx, msgs)
            summary = (resp.get('message') or {}).get('content') or ""
        except Exception as e:
            ctx.log(f"上下文压缩失败: {e}", level="warning")

        await ctx.db_execute_async(
            "DELETE FROM llm_dev_messages WHERE user_id = %s AND code = %s",
            (str(user_id), str(code)),
        )
        if summary:
            await ctx.db_execute_async(
                "INSERT INTO llm_dev_messages (user_id, code, role, content) VALUES (%s, %s, 'system', %s)",
                (str(user_id), str(code), "【自动压缩的历史摘要】" + summary[:char_limit]),
            )
        for r in keep:
            await ctx.db_execute_async(
                "INSERT INTO llm_dev_messages (user_id, code, role, content) VALUES (%s, %s, %s, %s)",
                (str(user_id), str(code), str(r.get('role') or 'user'), str(r.get('content') or '')),
            )
        ctx.log(f"[AI助手] 会话 #{code} 已自动压缩: {len(old)} 条旧消息 → 摘要", level="info")
    except Exception as e:
        ctx.log(f"压缩检查失败: {e}", level="warning")


async def _record_usage(ctx, user_id, usage: dict):
    """记录一次调用的 token 消耗"""
    if not usage:
        return
    try:
        await ctx.db_execute_async(
            "INSERT INTO llm_dev_usage (user_id, prompt_tokens, completion_tokens, total_tokens) "
            "VALUES (%s, %s, %s, %s)",
            (str(user_id),
             int(usage.get('prompt_tokens') or 0),
             int(usage.get('completion_tokens') or 0),
             int(usage.get('total_tokens') or 0)),
        )
    except Exception as e:
        ctx.log(f"记录用量失败: {e}", level="warning")


async def _usage_summary(ctx, user_id) -> str:
    """返回用户累计用量文本"""
    try:
        rows = await ctx.db_query_async(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS pt, "
            "COALESCE(SUM(completion_tokens),0) AS ct, COALESCE(SUM(total_tokens),0) AS tt "
            "FROM llm_dev_usage WHERE user_id = %s",
            (str(user_id),),
        )
        if not rows:
            return ""
        r = rows[0]
        return f"\nⓘ 累计: {r.get('calls', 0)} 次调用, {r.get('tt', 0)} tokens (输入{r.get('pt', 0)}/输出{r.get('ct', 0)})"
    except Exception:
        return ""


def _clip(text, limit=_SESSION_MAX_CHARS):
    text = str(text or '')
    return text if len(text) <= limit else text[:limit] + f"...（截断，共{len(text)}字）"


# ---------------------------------------------------------------- LLM 调用

def _llm_headers(ctx) -> dict:
    api_key = str(_get_config(ctx, "api_key", "")).strip()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _llm_payload(ctx, messages, tools=None, tool_choice=None) -> dict:
    base_url = str(_get_config(ctx, "base_url", "")).strip()
    model = str(_get_config(ctx, "model", "gpt-4o-mini")).strip() or "gpt-4o-mini"
    temperature = float(_get_config(ctx, "temperature", 0.3) or 0.3)
    max_tokens = int(_get_config(ctx, "max_tokens", 8192) or 8192)
    if not base_url:
        raise RuntimeError("未配置 base_url，请在 Web UI 插件配置中设置")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice
    return payload


async def _chat_once(ctx, messages, tools=None, tool_choice=None) -> dict:
    """单次调用（httpx 异步），返回 {"message":..., "usage":{...}}"""
    import httpx  # 懒加载：仅在真正发起 LLM 请求时引入（省 ~2-5MB 常驻内存）
    base_url = str(_get_config(ctx, "base_url", "")).strip().rstrip('/')
    url = base_url + "/chat/completions"
    payload = _llm_payload(ctx, messages, tools=tools, tool_choice=tool_choice)
    # 每次新建 AsyncClient：避免跨事件循环复用被关闭的客户端（插件可热重载）
    # connect 限时 10s：避免网络抖动时长时间挂起；ConnectError/ConnectTimeout 自动重试（1s/3s 退避）
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        last_exc = None
        for attempt in range(3):
            try:
                resp = await client.post(url, headers=_llm_headers(ctx), json=payload)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                last_exc = e
                if attempt < 2:
                    await asyncio.sleep(1 if attempt == 0 else 3)
        else:
            raise last_exc
    if resp.status_code != 200:
        raise RuntimeError(f"LLM 接口返回 HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"LLM 响应解析失败: {e} - {str(data)[:300]}")
    usage = data.get("usage") or {}
    return {"message": message, "usage": usage}


async def _chat_with_tools(ctx, messages: list, max_rounds: int = 0, progress_cb=None):
    """
    带函数调用的对话循环（全异步，不占线程池）：
    1. 发送 messages + tools
    2. 若返回 tool_calls → 依次执行工具（to_thread 内同步 IO）→ 追加结果 → 继续
    3. 无 tool_calls → 返回 ("done", 文本, usage)
    4. 工具 ask_user → 返回 ("ask", question, options, usage)，由上层发给用户等待批准
    max_rounds=0 表示不限制轮数（可配置 max_tool_rounds）。
    每轮 assistant 的说明文本会通过 progress_cb 实时发送给用户。
    """
    max_rounds = int(_get_config(ctx, "max_tool_rounds", 0) or 0)
    total_usage = {}
    rounds = 0
    while True:
        rounds += 1
        if max_rounds and rounds > max_rounds:
            raise RuntimeError(f"函数调用超过 {max_rounds} 轮仍未结束")
        result = await _chat_once(ctx, messages, tools=_TOOLS)
        msg = result["message"]
        # 累计 usage
        u = result.get("usage") or {}
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            total_usage[k] = total_usage.get(k, 0) + int(u.get(k) or 0)

        content = msg.get("content")
        # 日志与进度：让用户看到 AI 的思考/说明
        if content:
            ctx.log(f"[AI助手] 第{rounds}轮说明: {_truncate(content, 200)}", level="info")
            if progress_cb:
                try:
                    await progress_cb(content)
                except Exception:
                    pass

        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return ("done", content or "", {}, total_usage)

        # 把 assistant 消息（含 tool_calls）加入历史
        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            ctx.log(f"[AI助手] 调用工具 {name} 参数={json.dumps(args, ensure_ascii=False)[:300]}", level="info")
            try:
                # 文件/插件操作放到线程池，避免阻塞事件循环
                result_txt = await asyncio.to_thread(_execute_tool, ctx, name, args)
            except Exception as e:
                result_txt = f"错误: {e}"
            # ask_user：停止循环，把问题交回上层发给用户
            if isinstance(result_txt, dict) and result_txt.get("__ask_user__"):
                question = str(result_txt.get("__ask_user__") or "").strip()
                options = result_txt.get("options") or []
                if not question:
                    question = "（AI 未填写问题内容）"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": f"已向用户提问并等待回复。问题: {question}，选项: {options}",
                })
                return ("ask", question, options, total_usage)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": str(result_txt),
            })
            # 每次工具结果返回后给一个极短让出，避免独占事件循环
            await asyncio.sleep(0)


# ---------------------------------------------------------------- 工具实现

def _resolve_path(ctx, path: str) -> str:
    """把相对路径解析到 cwd 基准目录，绝对路径直接用"""
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(_get_cwd(ctx), path))


def _execute_tool(ctx, name: str, args: dict):
    """执行 LLM 请求的工具调用"""
    # 框架更新工具（检查/更新框架源码，独立于文件路径）
    if name in ("check_framework_update", "update_framework"):
        return _execute_framework_tool(ctx, name, args)

    # 插件管理工具（不依赖 path）
    if name in ("load_plugin", "unload_plugin", "reload_plugin", "list_plugins"):
        return _execute_plugin_tool(ctx, name, args)

    if name == "ask_user":
        question = str(args.get("question") or "").strip()
        if not question:
            return "错误: 缺少 question 参数"
        options = args.get("options") or []
        if not isinstance(options, list):
            options = []
        options = [str(o) for o in options if str(o).strip()][:4]
        return {"__ask_user__": question, "options": options}

    # 文件操作工具：必须提供 path
    path = str(args.get("path") or "").strip()
    if not path:
        return "错误: 缺少 path 参数"

    if name == "ls":
        target = _resolve_path(ctx, path)
        if os.path.isfile(target):
            with open(target, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            return f"[文件] {target} ({len(content)} 字符)\n" + content[:4000]
        if os.path.isdir(target):
            try:
                entries = sorted(os.listdir(target))
            except OSError as e:
                return f"错误: {e}"
            lines = [f"[目录] {target} ({len(entries)} 项)"]
            for e in entries:
                full = os.path.join(target, e)
                mark = '/' if os.path.isdir(full) else ''
                lines.append(f"  {e}{mark}")
            return "\n".join(lines)
        # 支持通配符
        import glob
        matches = sorted(glob.glob(target))
        if matches:
            lines = [f"[匹配 {len(matches)} 项] {path}"]
            for m in matches:
                mark = '/' if os.path.isdir(m) else ''
                lines.append(f"  {m}{mark}")
            return "\n".join(lines)
        return f"未找到: {path}"

    if name == "write":
        target = _resolve_path(ctx, path)
        content = str(args.get("content") or "")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"已写入 {target} ({len(content)} 字符)"

    if name == "edit":
        target = _resolve_path(ctx, path)
        if not os.path.isfile(target):
            return f"错误: 文件不存在 {target}"
        old_text = str(args.get("old_text") or "")
        new_text = str(args.get("new_text") or "")
        if not old_text:
            return "错误: 缺少 old_text"
        with open(target, 'r', encoding='utf-8') as f:
            content = f.read()
        if old_text not in content:
            return "错误: 未找到要替换的文本（old_text 须精确匹配）"
        new_content = content.replace(old_text, new_text, 1)
        with open(target, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return f"已修改 {target}"

    if name == "rm":
        target = _resolve_path(ctx, path)
        if os.path.isdir(target):
            import shutil
            shutil.rmtree(target, ignore_errors=True)
            return f"已删除目录: {target}"
        if os.path.isfile(target):
            os.remove(target)
            return f"已删除文件: {target}"
        return f"未找到: {target}"

    if name == "read":
        target = _resolve_path(ctx, path)
        if not os.path.isfile(target):
            return f"错误: 文件不存在 {target}"
        start = max(1, int(args.get("start") or 1))
        limit = max(1, int(args.get("limit") or 200))
        with open(target, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        total = len(lines)
        if start > total:
            return f"[文件] {target} 共 {total} 行，起始行 {start} 超出范围"
        chunk = lines[start - 1:start - 1 + limit]
        out = [f"{start + i}  {ln.rstrip()}" for i, ln in enumerate(chunk)]
        text = "\n".join(out)
        head = f"[文件] {target} 共{total}行，显示{start}~{min(start + limit - 1, total)}行"
        return head + "\n" + text[:4000]

    if name == "search":
        target = _resolve_path(ctx, path)
        pattern = str(args.get("pattern") or "")
        globpat = str(args.get("glob") or "")
        if not pattern:
            return "错误: 缺少 pattern 参数"
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"错误: 正则无效 {e}"
        files = []
        if os.path.isfile(target):
            files = [target]
        elif os.path.isdir(target):
            for root, _dirs, fnames in os.walk(target):
                for fn in fnames:
                    if globpat and not fnmatch.fnmatch(fn, globpat):
                        continue
                    files.append(os.path.join(root, fn))
                    if len(files) >= 300:
                        break
                if len(files) >= 300:
                    break
        else:
            return f"未找到: {path}"
        hits = []
        for f in files:
            try:
                with open(f, 'r', encoding='utf-8', errors='replace') as fh:
                    for lineno, line in enumerate(fh, 1):
                        if rx.search(line):
                            hits.append(f"{f}:{lineno}: {line.rstrip()[:200]}")
                            if len(hits) >= 100:
                                break
            except Exception:
                continue
            if len(hits) >= 100:
                break
        if not hits:
            return f"无匹配: {pattern}"
        return f"[匹配 {len(hits)} 处] {pattern}\n" + "\n".join(hits)

    if name == "mkdir":
        target = _resolve_path(ctx, path)
        os.makedirs(target, exist_ok=True)
        return f"已创建目录: {target}"

    if name == "append":
        target = _resolve_path(ctx, path)
        content = str(args.get("content") or "")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'a', encoding='utf-8') as f:
            f.write(content)
        return f"已追加 {len(content)} 字符到 {target}"

    if name == "rename":
        target = _resolve_path(ctx, path)
        new_path = str(args.get("new_path") or "").strip()
        if not new_path:
            return "错误: 缺少 new_path"
        dest = _resolve_path(ctx, new_path)
        if not os.path.exists(target):
            return f"错误: 原路径不存在 {target}"
        try:
            os.rename(target, dest)
        except OSError as e:
            return f"错误: {e}"
        return f"已重命名: {target} → {dest}"

    return f"错误: 未知工具 {name}"


# ---------------------------------------------------------------- 框架更新工具

def _fw_project_root() -> str:
    """项目根目录：plugins/llm_plugin_gen/main.py → 项目根"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fw_parse_ver(v: str):
    """版本字符串 → 可比较元组（提取所有数字段），如 0.0.1-alpha.1-build.5 → (0,0,1,1,5)"""
    if not v:
        return None
    nums = re.findall(r'\d+', v)
    return tuple(int(n) for n in nums) if nums else None


def _fw_url_candidates(url: str) -> list:
    """生成候选下载地址（按优先级）：配置的加速代理 → 内置 ghproxy 镜像 → 直连 GitHub"""
    candidates = []
    proxy = ''
    try:
        proxy = str(ctx_get_proxy() or '').strip().rstrip('/')
    except Exception:
        proxy = ''
    if proxy:
        candidates.append(f"{proxy}/https://{url}")
    for mirror_host in ('ghproxy.net', 'ghproxy.cn'):
        candidates.append(f"https://{mirror_host}/https://{url}")
    candidates.append(url)
    seen, out = set(), []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def ctx_get_proxy() -> str:
    """读取框架配置的 GitHub 加速代理地址（config.yaml github_proxy）"""
    try:
        from framework import config as fw_config
        return fw_config.get('github_proxy', '')
    except Exception:
        return ''


def _execute_framework_tool(ctx, name: str, args: dict):
    """执行框架更新工具：check_framework_update（检查更新）/ update_framework（更新框架）"""
    import shutil
    import tempfile
    import time
    import zipfile

    import requests

    repo = 'kuangxing6367/zcbot'
    branch = 'main'
    root = _fw_project_root()

    # 本地版本
    local_ver = ''
    try:
        with open(os.path.join(root, 'VERSION'), 'r', encoding='utf-8') as f:
            local_ver = f.read().strip()
    except Exception:
        pass

    # ---- 检查更新 ----
    if name == "check_framework_update":
        release = None
        try:
            rresp = requests.get(
                f"https://api.github.com/repos/{repo}/releases?per_page=30",
                headers={'Accept': 'application/vnd.github+json'},
                timeout=15,
            )
            if rresp.status_code == 200:
                releases = rresp.json()
                if isinstance(releases, list) and releases:
                    best, best_t = None, None
                    for rel in releases:
                        t = rel.get('tag_name') or ''
                        tv = _fw_parse_ver(t[1:] if t.startswith('v') else t)
                        if tv is None:
                            continue
                        if best_t is None or tv > best_t:
                            best, best_t = rel, tv
                    release = best
        except Exception as e:
            return f"检查框架更新失败: {e}\n本地版本: {local_ver or '未知'}"

        if not release:
            return f"检查框架更新失败: 无法获取 GitHub Release（网络或仓库问题）\n本地版本: {local_ver or '未知'}"

        tag = release.get('tag_name', '') or ''
        remote_ver = tag[1:] if tag.startswith('v') else tag
        lt, rt = _fw_parse_ver(local_ver), _fw_parse_ver(remote_ver)
        if lt is not None and rt is not None:
            n = max(len(lt), len(rt))
            has_update = (rt + (0,) * (n - len(rt))) > (lt + (0,) * (n - len(lt)))
        else:
            has_update = None
        body = release.get('body') or ''
        name_msg = (release.get('name') or '').strip()
        commit_msg = name_msg or (body.split('\n')[0] if body else '') or f"Release {tag}"
        state = "有新版本 ✅" if has_update else ("已是最新 ✅" if has_update is False else "无法判断 ⚠️")
        return (
            f"框架更新检查结果：\n"
            f"- 本地版本: {local_ver or '未知'}\n"
            f"- 最新版本: {remote_ver or '未知'}（{state}）\n"
            f"- 发布说明: {commit_msg}\n"
            f"- 发布时间: {release.get('published_at', '未知')}\n"
            f"如需更新请回复继续，我会调用 update_framework 执行（自动备份后覆盖，更新后需重启生效）。"
        )

    # ---- 更新框架 ----
    if name == "update_framework":
        confirm = str(args.get("confirm") or "").strip().lower()
        if confirm not in ("yes", "true", "1", "确认", "y"):
            return (
                "⚠️ 更新框架会覆盖 framework/web/main.py 等代码文件（保留 plugins/、data/、config.yaml），"
                "更新后需重启框架生效。若确认执行，请以 confirm='yes' 再次调用 update_framework。"
            )

        # 1) 下载最新代码 ZIP（代理 → 镜像 → 直连）
        zip_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
        tmp_zip = None
        last_err = ''
        for zurl in _fw_url_candidates(zip_url):
            try:
                resp = requests.get(zurl, timeout=180, stream=True)
            except Exception as e:
                last_err = str(e)
                continue
            if resp.status_code == 404:
                return f"更新失败: 仓库或分支不存在 {repo}@{branch}"
            if resp.status_code != 200:
                last_err = f'HTTP {resp.status_code}'
                continue
            tmp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
            for chunk in resp.iter_content(chunk_size=8192):
                tmp_zip.write(chunk)
            tmp_zip.close()
            break
        if tmp_zip is None:
            return f"更新失败: 下载失败 {last_err}"

        try:
            # 2) 解压到临时目录
            tmp_dir = tempfile.mkdtemp(prefix='zcbot_fw_')
            try:
                with zipfile.ZipFile(tmp_zip.name, 'r') as zf:
                    zf.extractall(tmp_dir)
            except zipfile.BadZipFile:
                return "更新失败: 下载的 ZIP 文件无效"

            entries = [e for e in os.listdir(tmp_dir) if os.path.isdir(os.path.join(tmp_dir, e))]
            src_root = os.path.join(tmp_dir, entries[0]) if entries else tmp_dir

            # 3) 备份旧 framework 目录
            backup_dir = os.path.join(root, 'data', 'backups')
            os.makedirs(backup_dir, exist_ok=True)
            fw_backup = os.path.join(backup_dir, f'framework.{int(time.time())}')
            old_fw = os.path.join(root, 'framework')
            if os.path.isdir(old_fw):
                shutil.copytree(old_fw, fw_backup)

            # 4) 覆盖白名单内的代码/配置文件（用户数据一律跳过）
            include = {
                'framework', 'web', 'sql', 'main.py', 'requirements.txt',
                'start.sh', '.gitignore', 'README.md', 'LICENSE', 'VERSION',
            }
            updated = []
            for item in os.listdir(src_root):
                if item not in include:
                    continue
                src = os.path.join(src_root, item)
                dst = os.path.join(root, item)
                if os.path.isdir(src):
                    if os.path.isdir(dst):
                        shutil.rmtree(dst, ignore_errors=True)
                    shutil.copytree(src, dst)
                elif os.path.isfile(src):
                    os.makedirs(os.path.dirname(dst), exist_ok=True) if os.path.dirname(dst) else None
                    shutil.copy2(src, dst)
                updated.append(item)

            # 5) 清理临时文件
            try:
                os.unlink(tmp_zip.name)
            except Exception:
                pass
            shutil.rmtree(tmp_dir, ignore_errors=True)

            return (
                f"✅ 框架已更新（{len(updated)} 项: {', '.join(updated)}）\n"
                f"📦 旧代码已备份到 data/backups/{os.path.basename(fw_backup)}\n"
                f"⚠️ 请重启框架生效（可对 AI 说「重启框架」或手动重启）。"
            )
        finally:
            try:
                if tmp_zip and os.path.isfile(tmp_zip.name):
                    os.unlink(tmp_zip.name)
            except Exception:
                pass

    return f"错误: 未知工具 {name}"


def _execute_plugin_tool(ctx, name: str, args: dict):
    """执行插件管理工具（load/unload/reload/list）"""
    if name == "list_plugins":
        plugins = ctx._framework.plugin_loader.get_loaded_plugins()
        if not plugins:
            return "当前没有已加载的插件"
        lines = ["已加载插件:"]
        for n, info in sorted(plugins.items()):
            meta = info.get('meta', {})
            lines.append(f"- {meta.get('name', n)} ({n}) v{meta.get('version', '?')}")
        return "\n".join(lines)

    plugin_name = str(args.get("plugin_name") or "").strip()
    if not plugin_name or not re.match(r'^[a-zA-Z0-9_\-]+$', plugin_name):
        return f"错误: 非法插件名 {plugin_name!r}（须为英文/数字/下划线/短横线）"

    if name == "load_plugin":
        loader = ctx._framework.plugin_loader
        main_path = os.path.join(loader.plugins_dir, plugin_name, 'main.py')
        if not os.path.isfile(main_path):
            return f"错误: 未找到 {main_path}，请先用 write 创建插件代码"
        try:
            with open(main_path, 'r', encoding='utf-8') as f:
                ast.parse(f.read())
        except SyntaxError as e:
            return f"错误: main.py 语法错误: {e}"
        try:
            ok = loader.load_plugin(plugin_name)
        except Exception as e:
            return f"错误: 加载异常: {e}"
        if not ok:
            return "错误: 加载失败，请检查代码或依赖（可 ls 查看，edit 修改，reload_plugin 重试）"
        try:
            loader.register_commands(plugin_name)
            ctx._framework.router._invalidate_cache()
        except Exception:
            pass
        meta = loader.get_loaded_plugins().get(plugin_name, {}).get('meta', {})
        return f"✅ 插件已加载: {meta.get('name', plugin_name)} v{meta.get('version', '?')}"

    if name == "unload_plugin":
        try:
            ctx._framework.plugin_loader.unload_plugin(plugin_name)
        except Exception as e:
            return f"错误: {e}"
        return f"已卸载插件: {plugin_name}"

    if name == "reload_plugin":
        loader = ctx._framework.plugin_loader
        try:
            loader.unload_plugin(plugin_name)
            ok = loader.load_plugin(plugin_name)
            if not ok:
                return "错误: 重载失败（代码可能有问题）"
            loader.register_commands(plugin_name)
            ctx._framework.router._invalidate_cache()
        except Exception as e:
            return f"错误: 重载异常: {e}"
        return f"✅ 插件已重载: {plugin_name}"

    return f"错误: 未知工具 {name}"


# ---------------------------------------------------------------- handlers

def _truncate(text, limit=1800):
    return text if len(text) <= limit else text[:limit] + f"...（已截断，共 {len(text)} 字）"


def _get_prompt(event, match, cmd_prefix):
    prompt = ""
    if match:
        prompt = match.group(1).strip()
    if not prompt:
        msg = event.message or ""
        if msg.startswith(cmd_prefix):
            prompt = msg[len(cmd_prefix):].strip()
    return prompt


def _parse_code(text) -> str or None:
    """解析 #13 / 13 → '13'；无法解析返回 None"""
    text = (text or "").strip()
    m = re.fullmatch(r'#?(\d{1,2})', text)
    return m.group(1) if m else None


def _loaded_plugins_text(ctx) -> str:
    """当前已加载插件列表（注入对话上下文）"""
    plugins = ctx._framework.plugin_loader.get_loaded_plugins()
    if not plugins:
        return "（当前没有已加载的插件）"
    lines = ["当前已加载插件："]
    for n, info in sorted(plugins.items()):
        meta = info.get('meta', {})
        lines.append(f"- {meta.get('name', n)} ({n}) v{meta.get('version', '?')}")
    return "\n".join(lines)


async def _send(ctx, event, message):
    await ctx.asend_msg(user_id=event.user_id,
                        group_id=event.group_id if event.is_group else None,
                        message=message)


# ---- 会话子命令 ----

async def _cmd_new(ctx, event, title=None):
    user_id = str(event.user_id)
    code = await _create_conversation(ctx, user_id, title)
    text = f"✅ 已创建会话 #{code} 并自动进入。"
    if title:
        text += f"\n标题: {title}"
    text += "\n直接发 /ai <需求> 开始；/ai list 查看所有会话；/ai del 删除本会话"
    await _send(ctx, event, text)


async def _cmd_list(ctx, event):
    user_id = str(event.user_id)
    try:
        rows = await ctx.db_query_async(
            "SELECT code, title, status, created_at FROM llm_dev_conversations "
            "WHERE user_id = %s ORDER BY id ASC", (user_id,))
    except Exception as e:
        ctx.log(f"列出会话失败: {e}", level="warning")
        rows = []
    if not rows:
        await _send(ctx, event, "📭 还没有会话。用 /ai new 创建第一个会话。")
        return
    current = await _get_current_code(ctx, user_id)
    lines = [f"📋 会话列表（{len(rows)} 个，当前 #{current or '无'}）:"]
    for r in rows:
        code = str(r.get('code') or '')
        title = str(r.get('title') or '').strip()
        status = str(r.get('status') or 'idle')
        mark = "▶" if code == current else " "
        lines.append(f"{mark} #{code}  {title or '(无标题)'}  [{status}]")
    lines.append("\n/ai set #编号 进入  |  /ai del #编号 删除")
    await _send(ctx, event, "\n".join(lines))


async def _cmd_set(ctx, event, code=None):
    user_id = str(event.user_id)
    if not code:
        await _send(ctx, event, "用法: /ai set #编号")
        return
    conv = await _get_conversation(ctx, user_id, code)
    if not conv:
        await _send(ctx, event, f"会话 #{code} 不存在（/ai list 查看）")
        return
    await _set_current(ctx, user_id, code)
    title = str(conv.get('title') or '').strip()
    text = f"✅ 已进入会话 #{code}。" + (f"（{title}）" if title else "")
    text += "\n直接发 /ai <需求> 开始。"
    await _send(ctx, event, text)


async def _cmd_del(ctx, event, code=None):
    user_id = str(event.user_id)
    if not code:
        code = await _get_current_code(ctx, user_id)
        if not code:
            await _send(ctx, event, "当前没有会话可删除。用法: /ai del [#编号]")
            return
    conv = await _get_conversation(ctx, user_id, code)
    if not conv:
        await _send(ctx, event, f"会话 #{code} 不存在（/ai list 查看）")
        return
    await _delete_conversation(ctx, user_id, code)
    # 若删的是当前会话，清空 current
    current = await _get_current_code(ctx, user_id)
    if current == code:
        await _set_current(ctx, user_id, None)
    await _send(ctx, event, f"🗑 会话 #{code} 已删除。")


async def _cmd_stop(ctx, event, code=None):
    """暂停指定会话（默认当前会话）"""
    user_id = str(event.user_id)
    if not code:
        code = await _get_current_code(ctx, user_id)
        if not code:
            await _send(ctx, event, "当前没有会话。")
            return
    key = (user_id, code)
    task = _active_tasks.get(key)
    if task and not task.done():
        task.cancel()
        await _send(ctx, event, f"⏹ 已请求暂停会话 #{code}，AI 正在停止当前操作...")
    else:
        await _send(ctx, event, f"会话 #{code} 当前没有正在运行的任务。")


async def _cmd_continue(ctx, event, text):
    """非破坏性纠错：/ai continue 修改需求：xxxx → 基于当前进度修改后续步骤"""
    user_id = str(event.user_id)
    code = await _get_current_code(ctx, user_id)
    if not code:
        await _send(ctx, event, "当前没有会话，请先用 /ai new 创建，或直接 /ai <需求> 开始。")
        return
    key = (user_id, code)
    # 停止正在运行的任务，避免新旧任务并发
    task = _active_tasks.get(key)
    if task and not task.done():
        task.cancel()
    # 若正等待用户确认（有未完成上下文），取出并注入修改指令后继续
    info = _pending_ask.pop(key, None)
    if info:
        await _append_session(ctx, user_id, code, "user", f"（用户中途修改需求）{text}")
        msgs = list(info["msgs"])
        msgs.append({"role": "user", "content": f"（用户中途修改需求）{text}"})
        await _send(ctx, event, f"🔄 已收到修改需求，AI 将基于会话 #{code} 当前进度调整后续步骤...")
        await _start_ai_ctx(ctx, event, code, msgs)
    else:
        await _start_ai(ctx, event, code, f"（用户中途修改需求）{text}")


# ---- 对话主流程 ----

async def _start_ai(ctx, event, code, prompt):
    """发起一次 AI 请求（创建 task 后台运行，便于 /ai stop 取消）"""
    user_id = str(event.user_id)
    if not code:
        code = await _get_current_code(ctx, user_id)
        if not code:
            code = await _create_conversation(ctx, user_id, None)
            await _send(ctx, event, f"✅ 已自动创建会话 #{code}。")
    else:
        # 指定编号但会话已失效（被自动回收等）→ 重建
        if not await _get_conversation(ctx, user_id, code):
            await _send(ctx, event, f"会话 #{code} 已不存在，已重新创建。")
            await _delete_conversation(ctx, user_id, code)
            code = await _create_conversation(ctx, user_id, None)
    if not str(_get_config(ctx, "base_url", "")).strip():
        await _send(ctx, event, "尚未配置 LLM 接口，请在 Web UI → 插件配置 中设置 base_url / api_key / model。")
        return
    await _send(ctx, event, f"🤖 会话 #{code} 已收到指令，AI 正在处理（每个节点会实时汇报进度）...\n{_truncate(prompt, 200)}")
    task = asyncio.create_task(_run_ai(ctx, event, code, prompt))
    _active_tasks[(user_id, code)] = task
    # 清理回调：仅当表中仍是本任务时才移除，避免旧任务取消时误删新任务
    task.add_done_callback(
        lambda t: _active_tasks.pop((user_id, code), None)
        if _active_tasks.get((user_id, code)) is t else None
    )


async def _run_ai(ctx, event, code, prompt, resumed_msgs=None):
    """AI 主循环：加载历史 → 工具循环（ask_user 门控 / 进度推送）→ 汇报"""
    user_id = str(event.user_id)
    key = (user_id, code)
    try:
        await _set_status(ctx, user_id, code, "running")
        if prompt:
            await _append_session(ctx, user_id, code, "user", prompt)
        # 上下文超限自动压缩
        await _maybe_compress(ctx, user_id, code)

        if resumed_msgs is not None:
            msgs = resumed_msgs
        elif key in _pending_ask:
            # 会话正处于 waiting 状态：沿用未完成的上下文，并把新指令并入
            info = _pending_ask.pop(key)
            msgs = list(info["msgs"])
            if prompt:
                msgs.append({"role": "user", "content": f"（用户新指令）{prompt}"})
        else:
            system = _build_system_prompt(ctx, code)
            history = await _get_session(ctx, user_id, code)
            msgs = [{"role": "system", "content": system}, *history]
            user_content = (
                f"{_loaded_plugins_text(ctx)}\n\n"
                f"用户指令：{prompt}\n\n"
                "（提示：写新插件前先 ls plugins/llm_plugin_gen/docs/INDEX.md 读文档索引，"
                "再按需 ls 具体文档；遵循四阶段工作流：先澄清疑点 → 拆解 6 项待办并经用户确认 → "
                "逐项执行并广播进度；确认前禁止修改文件；简单需求直接做，不要过度思考）"
            )
            msgs.append({"role": "user", "content": user_content})

        # 进度回调：每轮 AI 说明文本实时发到群里
        async def progress(text):
            await _send(ctx, event, f"[#{code}] {_truncate(text, 800)}")
            try:
                await ctx.db_execute_async(
                    "INSERT INTO llm_webui_progress (user_id, code, text) VALUES (%s, %s, %s)",
                    (user_id, code, _truncate(text, 2000)))
            except Exception:
                pass

        outcome = await _chat_with_tools(ctx, msgs, progress_cb=progress)

        if outcome[0] == "ask":
            _question, options, usage = outcome[1], outcome[2], outcome[3]
            _pending_ask[key] = {"msgs": msgs, "options": options, "question": _question}
            await _record_usage(ctx, user_id, usage)
            await _set_status(ctx, user_id, code, "waiting")
            text = f"🤖 会话 #{code} 需要你确认：\n{_question}"
            if options:
                text += "\n\n" + "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
            text += f"\n\n回复: /ai #{code} 序号  或  /ai #{code} say 补充说明"
            await _send(ctx, event, text)
            return

        _result = outcome[1] or "（AI 未返回文本内容）"
        usage = outcome[3] if len(outcome) > 3 else {}
        await _append_session(ctx, user_id, code, "assistant", _result)
        await _record_usage(ctx, user_id, usage)
        cost = ""
        if usage.get('total_tokens'):
            cost = f"\n[本次消耗 {usage.get('total_tokens')} tokens]"
        summary = await _usage_summary(ctx, user_id)
        await _set_status(ctx, user_id, code, "idle")
        await _send(ctx, event, _truncate(_result) + cost + summary)
    except asyncio.CancelledError:
        await _set_status(ctx, user_id, code, "idle")
        await _send(ctx, event, f"⏹ 会话 #{code} 已暂停。可直接发 /ai <需求> 继续。")
        raise
    except Exception as e:
        ctx.log(f"AI 处理失败: {e}\n{traceback.format_exc()}", level="error")
        await _set_status(ctx, user_id, code, "idle")
        await _send(ctx, event, f"❌ 会话 #{code} 处理失败: {_truncate(str(e), 500)}")
    finally:
        # 仅当登记的任务仍是本任务时才移除，避免旧任务（如被 /ai continue 取消）误删新任务
        if _active_tasks.get(key) is asyncio.current_task():
            _active_tasks.pop(key, None)


async def _reply_option(ctx, event, code, num):
    """用户选择选项后，把选择注入上下文并继续 AI"""
    user_id = str(event.user_id)
    key = (user_id, code)
    info = _pending_ask.get(key)
    if not info:
        await _send(ctx, event, f"会话 #{code} 当前没有待确认的问题，可直接 /ai #{code} <需求> 继续。")
        return
    _pending_ask.pop(key)
    options = info.get("options") or []
    opt = options[num - 1] if 1 <= num <= len(options) else f"选项 {num}"
    msgs = list(info["msgs"])
    msgs.append({"role": "user", "content": f"（用户回复 /ai #{code} {num}）用户选择：{num}. {opt}"})
    await _append_session(ctx, user_id, code, "user", f"（回复确认）用户选择：{num}. {opt}")
    await _start_ai_ctx(ctx, event, code, msgs)


async def _start_ai_ctx(ctx, event, code, msgs):
    """用已有消息上下文继续 AI（不追加用户指令）"""
    user_id = str(event.user_id)
    if not str(_get_config(ctx, "base_url", "")).strip():
        await _send(ctx, event, "尚未配置 LLM 接口。")
        return
    task = asyncio.create_task(_run_ai(ctx, event, code, None, resumed_msgs=msgs))
    _active_tasks[(user_id, code)] = task
    task.add_done_callback(
        lambda t: _active_tasks.pop((user_id, code), None)
        if _active_tasks.get((user_id, code)) is t else None
    )


async def _reply_say(ctx, event, code, text):
    """用户自由补充后，把补充内容注入上下文并继续 AI"""
    user_id = str(event.user_id)
    key = (user_id, code)
    info = _pending_ask.get(key)
    if not info:
        await _send(ctx, event, f"会话 #{code} 当前没有待确认的问题，可直接 /ai #{code} <需求> 继续。")
        return
    _pending_ask.pop(key)
    msgs = list(info["msgs"])
    msgs.append({"role": "user", "content": f"（用户回复 /ai #{code} say）用户补充：{text}"})
    await _append_session(ctx, user_id, code, "user", f"（回复补充）{text}")
    await _start_ai_ctx(ctx, event, code, msgs)


async def _show_conversation(ctx, event, code):
    """/ai #编号：展示会话信息"""
    user_id = str(event.user_id)
    conv = await _get_conversation(ctx, user_id, code)
    if not conv:
        await _send(ctx, event, f"会话 #{code} 不存在（/ai list 查看）")
        return
    title = str(conv.get('title') or '').strip()
    status = str(conv.get('status') or 'idle')
    text = f"📌 会话 #{code}"
    if title:
        text += f"（{title}）"
    text += f" 状态: {status}"
    if (user_id, code) in _pending_ask:
        text += "\n⏳ 有一个问题在等你确认，回复 /ai #" + code + " 序号"
    text += "\n发 /ai #" + code + " <需求> 在此会话继续。"
    await _send(ctx, event, text)


async def handle_ai(event, match):
    """
    通用 AI 对话 + 会话管理。
    全异步：不占用框架线程池（httpx 异步 HTTP + db_*_async + asend_msg）
    """
    prompt = _get_prompt(event, match, "/ai")
    user_id = str(event.user_id)
    if not prompt:
        await _send(ctx, event, _USAGE_TEXT)
        return

    low = prompt.strip()

    # ---- 子命令 ----
    if low == "new" or low.startswith("new "):
        await _cmd_new(ctx, event, low[4:].strip() if len(low) > 3 else None)
        return
    if low == "list":
        await _cmd_list(ctx, event)
        return
    if low == "del" or low.startswith("del "):
        await _cmd_del(ctx, event, _parse_code(low[3:]))
        return
    if low == "set" or low.startswith("set "):
        await _cmd_set(ctx, event, _parse_code(low[3:]))
        return
    if low == "stop":
        await _cmd_stop(ctx, event)
        return
    if low == "continue" or low.startswith("continue "):
        text = low[len("continue"):].strip()
        if not text:
            await _send(ctx, event, "用法: /ai continue 修改需求：<新需求>")
            return
        await _cmd_continue(ctx, event, text)
        return
    if prompt in ("重置", "清空", "新会话", "重来"):
        await _cmd_del(ctx, event, None)
        return

    # ---- #编号 语法 ----
    m = re.match(r'^#(\d{1,2})(?:\s+(.*))?$', prompt)
    if m:
        code = m.group(1)
        rest = (m.group(2) or "").strip()
        if not rest:
            await _show_conversation(ctx, event, code)
            return
        if rest.lower() == "stop":
            await _cmd_stop(ctx, event, code)
            return
        if re.fullmatch(r'[1-4]', rest):
            await _reply_option(ctx, event, code, int(rest))
            return
        if rest.lower().startswith("say"):
            text = rest[3:].strip()
            if not text:
                await _send(ctx, event, f"用法: /ai #{code} say <补充内容>")
                return
            await _reply_say(ctx, event, code, text)
            return
        await _start_ai(ctx, event, code, rest)
        return

    # ---- 普通请求：当前会话（无则自动创建） ----
    await _start_ai(ctx, event, None, prompt)


async def handle_list_plugins(event, match):
    loader = ctx._framework.plugin_loader
    plugins = loader.get_loaded_plugins()
    if not plugins:
        text = "当前没有已加载的插件。"
    else:
        lines = ["📦 已加载插件:"]
        for name, info in sorted(plugins.items()):
            meta = info.get('meta', {})
            lines.append(f"- {meta.get('name', name)} ({name}) v{meta.get('version', '?')}")
        text = "\n".join(lines)
    await _send(ctx, event, text)

# ══════════════════════════════════════════════════════════
#  WebUI 后端 API（网页控制台：以超管 QQ 身份操作，与 QQ 群数据互通）
#  网页端身份 = users 表 role=super 的超管 QQ，因此网页与 QQ 群共享同一套会话
# ══════════════════════════════════════════════════════════

# 超管 QQ 缓存: {"qq": str|None, "ts": float}
_web_super_cache = {"qq": None, "ts": 0.0}


class _WebEvent:
    """网页端伪事件：以超管 QQ 身份执行（复用 QQ 端会话数据）"""

    __slots__ = ("user_id", "group_id", "is_group", "message", "message_id",
                 "is_admin", "is_superuser")

    def __init__(self, user_id):
        self.user_id = user_id
        self.group_id = None
        self.is_group = False
        self.message = ""
        self.message_id = 0
        self.is_admin = True
        self.is_superuser = True


def _resolve_super_qq():
    """解析网页操作绑定的超管 QQ（users 表 role=super 的第一个），60s 缓存"""
    now = time.time()
    if _web_super_cache["qq"] is not None and now - _web_super_cache["ts"] < 60:
        return _web_super_cache["qq"]
    qq = None
    try:
        db = ctx._framework.db
        row = db.query_one(
            "SELECT user_id FROM users WHERE role='super' ORDER BY id ASC LIMIT 1")
        if row:
            qq = str(row["user_id"])
    except Exception:
        pass
    _web_super_cache.update({"qq": qq, "ts": now})
    return qq


def _verify_web_token(token):
    """校验 token（逻辑与框架 apis.py 一致），返回 admin 字典或 None"""
    try:
        db = ctx._framework.db
        row = db.query_one(
            "SELECT id, username, role, is_active, token_created_at "
            "FROM admin_users WHERE token = %s", (token,))
        if not row or not row.get("is_active"):
            return None
        web_cfg = ctx._framework.config.get("web", {})
        timeout = web_cfg.get("token_timeout") or web_cfg.get("session_timeout", 86400)
        created = row.get("token_created_at")
        if created:
            if isinstance(created, str):
                try:
                    created = datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return None
            expiry = created + timedelta(seconds=timeout)
            if datetime.now() > expiry:
                return None
        return {"id": row["id"], "username": row["username"], "role": row["role"]}
    except Exception:
        return None


def _web_require_auth(fn):
    """WebUI API 鉴权装饰器（Bearer / Cookie 双通道，与框架一致）"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            token = ""
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:]
            else:
                token = request.cookies.get("zcbot_token") or ""
            if not token or len(token) != 2048:
                return jsonify({"code": 401, "msg": "未提供有效认证令牌"}), 401
            admin = _verify_web_token(token)
            if not admin:
                return jsonify({"code": 401, "msg": "令牌无效或已过期"}), 401
            request.admin = admin
        except Exception:
            return jsonify({"code": 401, "msg": "认证失败"}), 401
        return fn(*args, **kwargs)
    return wrapper


def _run_coro_in_loop(coro):
    """把协程调度到框架主事件循环执行（Web 线程 → 主循环），返回结果"""
    loop = getattr(ctx._framework, "loop", None)
    if loop is None or loop.is_closed():
        return asyncio.run(coro)
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=120)


def _is_running(user_id, code):
    """会话是否正在运行 AI 任务"""
    task = _active_tasks.get((str(user_id), str(code)))
    return bool(task and not task.done())


# ---- 查询接口 ----

@_web_require_auth
def _web_info():
    qq = _resolve_super_qq()
    return jsonify({"code": 0, "data": {
        "super_qq": qq,
        "llm_configured": bool(str(_get_config(ctx, "base_url", "")).strip()),
        "model": _get_config(ctx, "model", ""),
    }})


@_web_require_auth
def _web_sessions():
    qq = _resolve_super_qq()
    if not qq:
        return jsonify({"code": 403, "msg": "未配置超管 QQ（users 表 role=super）"}), 403
    try:
        rows = _run_coro_in_loop(_list_sessions_async(qq))
    except Exception:
        rows = []
    current = None
    try:
        current = _run_coro_in_loop(_get_current_code(ctx, qq))
    except Exception:
        pass
    for r in rows:
        code = str(r.get("code") or "")
        r["is_current"] = (code == current)
        r["waiting"] = (qq, code) in _pending_ask
        r["running"] = _is_running(qq, code)
    return jsonify({"code": 0, "data": {"current": current, "sessions": rows}})


async def _list_sessions_async(user_id):
    try:
        rows = await ctx.db_query_async(
            "SELECT code, title, status, created_at, last_active_at "
            "FROM llm_dev_conversations WHERE user_id = %s "
            "ORDER BY last_active_at DESC, id DESC", (str(user_id),))
        return rows or []
    except Exception:
        return []


@_web_require_auth
def _web_messages(code):
    qq = _resolve_super_qq()
    if not qq:
        return jsonify({"code": 403, "msg": "未配置超管 QQ"}), 403
    try:
        msgs = _run_coro_in_loop(_get_session(ctx, qq, code))
    except Exception:
        msgs = []
    conv = None
    try:
        conv = _run_coro_in_loop(_get_conversation(ctx, qq, code))
    except Exception:
        pass
    if conv is None:
        return jsonify({"code": 404, "msg": f"会话 #{code} 不存在"}), 404
    pending = _pending_ask.get((qq, code))
    return jsonify({"code": 0, "data": {
        "code": code,
        "conv": conv,
        "messages": msgs,
        "pending": {
            "question": str(pending.get("question") or ""),
            "options": pending.get("options") or [],
        } if pending else None,
        "running": _is_running(qq, code),
    }})


@_web_require_auth
def _web_progress(code):
    qq = _resolve_super_qq()
    if not qq:
        return jsonify({"code": 403, "msg": "未配置超管 QQ"}), 403
    try:
        items = _run_coro_in_loop(_get_progress_async(qq, code))
    except Exception:
        items = []
    return jsonify({"code": 0, "data": {"items": items}})


async def _get_progress_async(user_id, code):
    """读取 WebUI 进度并清理（仅保留最近 20 条，防止表无限增长）"""
    try:
        rows = await ctx.db_query_async(
            "SELECT id, text, created_at FROM llm_webui_progress "
            "WHERE user_id = %s AND code = %s ORDER BY id ASC",
            (str(user_id), str(code)))
        if rows:
            last_id = int(rows[-1]["id"])
            await ctx.db_execute_async(
                "DELETE FROM llm_webui_progress WHERE user_id = %s AND code = %s AND id < %s",
                (str(user_id), str(code), max(0, last_id - 20)))
        return rows or []
    except Exception:
        return []


# ---- 操作接口 ----

@_web_require_auth
def _web_session_create():
    qq = _resolve_super_qq()
    if not qq:
        return jsonify({"code": 403, "msg": "未配置超管 QQ"}), 403
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip() or None
    try:
        code = _run_coro_in_loop(_create_conversation(ctx, qq, title))
    except Exception as e:
        return jsonify({"code": 500, "msg": f"创建失败: {e}"}), 500
    return jsonify({"code": 0, "msg": f"已创建会话 #{code}", "data": {"code": code}})


@_web_require_auth
def _web_set_current():
    qq = _resolve_super_qq()
    if not qq:
        return jsonify({"code": 403, "msg": "未配置超管 QQ"}), 403
    data = request.get_json(silent=True) or {}
    code = str(data.get("code") or "").strip()
    if not code:
        return jsonify({"code": 400, "msg": "缺少会话编号"}), 400
    try:
        _run_coro_in_loop(_set_current(ctx, qq, code))
    except Exception as e:
        return jsonify({"code": 500, "msg": f"切换失败: {e}"}), 500
    return jsonify({"code": 0, "msg": f"已切换到会话 #{code}"})


@_web_require_auth
def _web_send(code):
    qq = _resolve_super_qq()
    if not qq:
        return jsonify({"code": 403, "msg": "未配置超管 QQ"}), 403
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()
    if not text:
        return jsonify({"code": 400, "msg": "指令内容不能为空"}), 400
    try:
        _run_coro_in_loop(_start_ai(ctx, _WebEvent(qq), code, text))
    except Exception as e:
        return jsonify({"code": 500, "msg": f"发送失败: {e}"}), 500
    return jsonify({"code": 0, "msg": "已提交，AI 正在处理"})


@_web_require_auth
def _web_option(code):
    qq = _resolve_super_qq()
    if not qq:
        return jsonify({"code": 403, "msg": "未配置超管 QQ"}), 403
    data = request.get_json(silent=True) or {}
    try:
        num = int(data.get("num") or 0)
    except (TypeError, ValueError):
        num = 0
    if not (1 <= num <= 4):
        return jsonify({"code": 400, "msg": "选项序号需为 1~4"}), 400
    try:
        _run_coro_in_loop(_reply_option(ctx, _WebEvent(qq), code, num))
    except Exception as e:
        return jsonify({"code": 500, "msg": f"回复失败: {e}"}), 500
    return jsonify({"code": 0, "msg": "已选择，AI 继续处理"})


@_web_require_auth
def _web_say(code):
    qq = _resolve_super_qq()
    if not qq:
        return jsonify({"code": 403, "msg": "未配置超管 QQ"}), 403
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()
    if not text:
        return jsonify({"code": 400, "msg": "补充内容不能为空"}), 400
    try:
        _run_coro_in_loop(_reply_say(ctx, _WebEvent(qq), code, text))
    except Exception as e:
        return jsonify({"code": 500, "msg": f"补充失败: {e}"}), 500
    return jsonify({"code": 0, "msg": "已补充，AI 继续处理"})


@_web_require_auth
def _web_continue():
    qq = _resolve_super_qq()
    if not qq:
        return jsonify({"code": 403, "msg": "未配置超管 QQ"}), 403
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()
    if not text:
        return jsonify({"code": 400, "msg": "修改需求不能为空"}), 400
    try:
        _run_coro_in_loop(_cmd_continue(ctx, _WebEvent(qq), text))
    except Exception as e:
        return jsonify({"code": 500, "msg": f"提交失败: {e}"}), 500
    return jsonify({"code": 0, "msg": "已提交修改需求"})


@_web_require_auth
def _web_stop(code):
    qq = _resolve_super_qq()
    if not qq:
        return jsonify({"code": 403, "msg": "未配置超管 QQ"}), 403
    try:
        _run_coro_in_loop(_cmd_stop(ctx, _WebEvent(qq), code))
    except Exception as e:
        return jsonify({"code": 500, "msg": f"停止失败: {e}"}), 500
    return jsonify({"code": 0, "msg": "已请求停止"})


@_web_require_auth
def _web_delete(code):
    qq = _resolve_super_qq()
    if not qq:
        return jsonify({"code": 403, "msg": "未配置超管 QQ"}), 403
    try:
        _run_coro_in_loop(_cmd_del(ctx, _WebEvent(qq), code))
    except Exception as e:
        return jsonify({"code": 500, "msg": f"删除失败: {e}"}), 500
    return jsonify({"code": 0, "msg": f"会话 #{code} 已删除"})


def _register_webui_routes(ctx_local):
    """向框架 Flask 应用动态注册 WebUI API 路由（reload 时幂等）"""
    try:
        app = ctx_local._framework.web_server.app
        if app is None:
            ctx_local.log("WebUI 路由注册失败: web_server.app 为空", level="warning")
            return
        routes = [
            ("/api/llm_webui/info", ["GET"], _web_info),
            ("/api/llm_webui/sessions", ["GET"], _web_sessions),
            ("/api/llm_webui/sessions", ["POST"], _web_session_create),
            ("/api/llm_webui/current", ["PUT"], _web_set_current),
            ("/api/llm_webui/sessions/<code>/messages", ["GET"], _web_messages),
            ("/api/llm_webui/sessions/<code>/progress", ["GET"], _web_progress),
            ("/api/llm_webui/sessions/<code>/send", ["POST"], _web_send),
            ("/api/llm_webui/sessions/<code>/option", ["POST"], _web_option),
            ("/api/llm_webui/sessions/<code>/say", ["POST"], _web_say),
            ("/api/llm_webui/continue", ["POST"], _web_continue),
            ("/api/llm_webui/sessions/<code>/stop", ["POST"], _web_stop),
            ("/api/llm_webui/sessions/<code>/delete", ["POST"], _web_delete),
        ]
        existing = {str(r.rule) for r in app.url_map.iter_rules()}
        added = 0
        for rule, methods, fn in routes:
            if rule not in existing:
                app.add_url_rule(rule, endpoint=f"llm_webui_{fn.__name__}",
                                 view_func=fn, methods=methods)
                added += 1
        ctx_local.log(f"WebUI API 路由注册完成（新增 {added} 条）")
    except Exception as e:
        ctx_local.log(f"WebUI 路由注册失败: {e}", level="error")

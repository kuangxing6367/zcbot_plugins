# -*- coding: utf-8 -*-
"""防护中心 · 数据存取层

统一负责三张表的读写：
- guard_block_users    全局用户黑名单（命中后消息被静默丢弃）
- guard_llm_block      LLM 对话拦截名单（命中后拒绝使用 LLM 对话功能）
- guard_spam_settings  群级刷屏配置（开关 / 阈值 / 禁言时长）

约定：
- 全部 SQL 一律 %s 参数化，兼容 MySQL / SQLite；
- 建表统一走 ctx.create_table（框架自动适配方言）；
- 写入采用「先查后写」，避免依赖特定数据库的 UPSERT 语法。
"""
import time

ctx = None  # 框架上下文，由 main.register 注入

# ── 建表语句 ──────────────────────────────────────────────

DDL_BLOCK_USERS = """
CREATE TABLE IF NOT EXISTS guard_block_users (
    user_id BIGINT PRIMARY KEY,
    reason VARCHAR(255) DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0
)
"""

# 注意：TEXT 列在 MySQL 下不允许有默认值、也不能作为主键，
#       因此 user_id 用 VARCHAR(64)，reason/operator 不设列默认值，
#       空串兜底由代码层保证（写入时永远带值）。
DDL_LLM_BLOCK = """
CREATE TABLE IF NOT EXISTS guard_llm_block (
    user_id VARCHAR(64) PRIMARY KEY,
    reason TEXT,
    operator TEXT,
    created_at INTEGER DEFAULT 0
)
"""

DDL_SPAM_SETTINGS = """
CREATE TABLE IF NOT EXISTS guard_spam_settings (
    group_id         BIGINT PRIMARY KEY,
    enabled          INT DEFAULT 1,
    user_threshold   INT DEFAULT 8,
    user_window      INT DEFAULT 5,
    user_mute        INT DEFAULT 300,
    group_threshold  INT DEFAULT 20,
    group_window     INT DEFAULT 5,
    group_mute       INT DEFAULT 600,
    exempt_admin     INT DEFAULT 1,
    notify           INT DEFAULT 1,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def init(ctx_obj):
    """注入框架上下文（main.register 调用）"""
    global ctx
    ctx = ctx_obj


def create_tables():
    """建全部表（幂等；任一失败抛异常由调用方兜底）"""
    for ddl in (DDL_BLOCK_USERS, DDL_LLM_BLOCK, DDL_SPAM_SETTINGS):
        ctx.create_table(ddl)


# ── 全局用户黑名单 ────────────────────────────────────────

async def add_block_user(uid: int, reason: str):
    """加入黑名单；已存在则只更新理由"""
    now = int(time.time())
    exists = await ctx.db_query_one_async(
        "SELECT 1 FROM guard_block_users WHERE user_id=%s", (uid,))
    if exists:
        await ctx.db_execute_async(
            "UPDATE guard_block_users SET reason=%s WHERE user_id=%s",
            (reason, uid))
    else:
        await ctx.db_execute_async(
            "INSERT INTO guard_block_users (user_id, reason, created_at) "
            "VALUES (%s,%s,%s)",
            (uid, reason, now))


async def remove_block_user(uid: int):
    """移出黑名单"""
    await ctx.db_execute_async(
        "DELETE FROM guard_block_users WHERE user_id=%s", (uid,))


async def list_block_users():
    """按加入时间倒序列出黑名单"""
    return await ctx.db_query_async(
        "SELECT user_id, reason FROM guard_block_users "
        "ORDER BY created_at DESC") or []


async def load_block_ids() -> set:
    """拉取全部黑名单 QQ（供拦截链内存缓存整体刷新）"""
    rows = await ctx.db_query_async(
        "SELECT user_id FROM guard_block_users") or []
    ids = set()
    for r in rows:
        try:
            ids.add(int(r["user_id"]))
        except (TypeError, ValueError, KeyError):
            continue
    return ids


# ── LLM 对话拦截名单 ──────────────────────────────────────

async def add_llm_block(uid: str, reason: str, operator: str):
    """加入 LLM 对话拦截名单；已存在则更新原因/操作人/时间"""
    now = int(time.time())
    exists = await ctx.db_query_one_async(
        "SELECT 1 FROM guard_llm_block WHERE user_id=%s", (uid,))
    if exists:
        await ctx.db_execute_async(
            "UPDATE guard_llm_block SET reason=%s, operator=%s, created_at=%s "
            "WHERE user_id=%s",
            (reason, operator, now, uid))
    else:
        await ctx.db_execute_async(
            "INSERT INTO guard_llm_block (user_id, reason, operator, created_at) "
            "VALUES (%s,%s,%s,%s)",
            (uid, reason, operator, now))


async def remove_llm_block(uid: str):
    """移出 LLM 对话拦截名单"""
    await ctx.db_execute_async(
        "DELETE FROM guard_llm_block WHERE user_id=%s", (uid,))


async def llm_block_exists(uid: str) -> bool:
    """查询某 QQ 是否仍在 LLM 对话拦截名单中"""
    row = await ctx.db_query_one_async(
        "SELECT 1 FROM guard_llm_block WHERE user_id=%s", (uid,))
    return bool(row)


async def list_llm_block():
    """按时间倒序列出 LLM 对话拦截名单"""
    return await ctx.db_query_async(
        "SELECT user_id, reason, operator, created_at FROM guard_llm_block "
        "ORDER BY created_at DESC") or []


def llm_gate_hit(user_id) -> bool:
    """对话闸门用同步查询（llm_core 以同步方式回调闸门）。

    数据库故障时返回 False（不拦截对话），与拦截链的容错原则一致。
    """
    try:
        row = ctx.db_query_one(
            "SELECT 1 FROM guard_llm_block WHERE user_id=%s", (str(user_id),))
        return bool(row)
    except Exception:
        return False


# ── 统计（仪表盘 / 总览命令用）────────────────────────────

async def count_block_users() -> int:
    """全局黑名单人数"""
    try:
        row = await ctx.db_query_one_async(
            "SELECT COUNT(*) AS cnt FROM guard_block_users")
        return int(row.get("cnt") or 0) if row else 0
    except Exception:
        return 0


async def count_llm_block() -> int:
    """LLM 对话拦截名单人数"""
    try:
        row = await ctx.db_query_one_async(
            "SELECT COUNT(*) AS cnt FROM guard_llm_block")
        return int(row.get("cnt") or 0) if row else 0
    except Exception:
        return 0

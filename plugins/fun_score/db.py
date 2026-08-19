"""
签到插件数据库模块（zgric_onebot11 框架适配版）
================================================

适配说明：
- 原版使用 aiomysql 连接池，本版改用 ctx.db_query()/ctx.db_execute()
- 原版 ScoreDB 和 UserDB 分别管理独立连接池，本版统一使用框架数据库连接
- 与原版保持相同的接口签名，确保 ban_word.py 等调用方无需修改

表结构：
- fun_user：用户数据总表（sid -> uid 映射，含 score/sign_count/last_sign_date/gold）
- fun_score_style：群默认签到样式表（group_id PK, style）
"""
from __future__ import annotations

import datetime
import logging

logger = logging.getLogger("fun_score")


class ScoreDB:
    """签到数据库操作（适配 ctx.db_query/ctx.db_execute）

    提供与原版 astrobot_plugin_fun_score.ScoreDB 相同的接口，
    但内部使用框架数据库连接而非独立 aiomysql 连接池。
    """

    def __init__(self, ctx):
        """
        :param ctx: 插件上下文（由 register() 注入的模块级 ctx）
        """
        self.ctx = ctx

    # ===================== 建表 =====================

    def init_tables(self):
        """创建/更新所需表结构（同步调用）"""
        try:
            # fun_score_style 表（群默认签到样式）
            self.ctx.db_execute("""
                CREATE TABLE IF NOT EXISTS fun_score_style (
                    group_id VARCHAR(20) PRIMARY KEY COMMENT '群号',
                    style INT NOT NULL DEFAULT 1 COMMENT '默认签到样式(0-3)'
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='群签到样式表';
            """)
        except Exception as e:
            logger.warning("创建 fun_score_style 表失败: %s", e)

        # 确保 fun_user 表有签到相关字段
        try:
            for col, definition in (
                ("score", "INT NOT NULL DEFAULT 0 COMMENT '积分'"),
                ("sign_count", "INT NOT NULL DEFAULT 0 COMMENT '累计签到次数'"),
                ("last_sign_date", "DATE DEFAULT NULL COMMENT '最后签到日期'"),
                ("gold", "INT NOT NULL DEFAULT 0 COMMENT '金币余额'"),
            ):
                try:
                    self.ctx.db_execute(
                        f"ALTER TABLE fun_user ADD COLUMN {col} {definition}"
                    )
                except Exception:
                    pass  # 字段已存在，忽略
        except Exception as e:
            logger.warning("添加 fun_user 签到字段失败: %s", e)

        logger.info("[ScoreDB] 表结构已就绪")

    # ===================== 用户信息 =====================

    def get_user_nickname(self, uid: int) -> str:
        """从 fun_user 获取用户昵称（uid 字段）"""
        if not uid:
            return str(uid)
        try:
            row = self.ctx.db_query_one(
                "SELECT uid FROM fun_user WHERE sid = %s", (str(uid),)
            )
            if row:
                return str(row.get("uid", str(uid)) if isinstance(row, dict) else row[0])
        except Exception as e:
            logger.debug("get_user_nickname(%s) 失败: %s", uid, e)
        return str(uid)

    def get_user_uuid(self, uid: int) -> str:
        """从 fun_user 获取用户 uuid"""
        if not uid:
            return ""
        try:
            row = self.ctx.db_query_one(
                "SELECT uuid FROM fun_user WHERE sid = %s", (str(uid),)
            )
            if row:
                return str(row.get("uuid", "") if isinstance(row, dict) else row[0])
        except Exception as e:
            logger.debug("get_user_uuid(%s) 失败: %s", uid, e)
        return ""

    # ===================== 群默认样式 =====================

    def get_group_style(self, group_id: str) -> int:
        """获取群默认签到样式，默认为 1"""
        if not group_id:
            return 1
        try:
            row = self.ctx.db_query_one(
                "SELECT style FROM fun_score_style WHERE group_id = %s",
                (str(group_id),),
            )
            if row:
                if isinstance(row, dict):
                    return int(row.get("style", 1))
                return int(row[0])
        except Exception as e:
            logger.debug("get_group_style(%s) 失败: %s", group_id, e)
        return 1

    def set_group_style(self, group_id: str, style: int) -> None:
        """设置群默认签到样式"""
        if not group_id:
            return
        try:
            self.ctx.db_execute(
                "INSERT INTO fun_score_style (group_id, style) "
                "VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE style = VALUES(style)",
                (str(group_id), int(style)),
            )
        except Exception as e:
            logger.warning("set_group_style(%s, %s) 失败: %s", group_id, style, e)


class UserDB:
    """fun_user 表操作（适配 ctx.db_query/ctx.db_execute）

    提供与原版 astrobot_plugin_fun_score.UserDB 相同的接口。
    """

    def __init__(self, ctx):
        """
        :param ctx: 插件上下文（由 register() 注入的模块级 ctx）
        """
        self.ctx = ctx

    # ===================== 用户注册/更新 =====================

    def register_or_update(self, sid: str, uid: str) -> None:
        """注册或更新用户信息（sid -> uid 映射）

        :param sid: 平台用户 ID（QQ 号）
        :param uid: 用户昵称
        """
        if not sid:
            return
        import time
        now_ts = int(time.time())
        try:
            self.ctx.db_execute(
                "INSERT INTO fun_user (sid, uid, uuid, first_seen, last_seen) "
                "VALUES (%s, %s, UUID(), %s, %s) "
                "ON DUPLICATE KEY UPDATE uid=VALUES(uid), last_seen=VALUES(last_seen)",
                (str(sid), str(uid), now_ts, now_ts),
            )
        except Exception as e:
            logger.warning("register_or_update(%s) 失败: %s", sid, e)

    # ===================== 签到数据 =====================

    def get_sign_info(self, sid: str | int) -> dict:
        """获取用户签到信息"""
        try:
            row = self.ctx.db_query_one(
                "SELECT sign_count, last_sign_date FROM fun_user WHERE sid=%s",
                (str(sid),),
            )
            if row:
                if isinstance(row, dict):
                    return {
                        "sign_count": int(row.get("sign_count") or 0),
                        "last_sign_date": row.get("last_sign_date"),
                    }
                return {"sign_count": int(row[0] or 0), "last_sign_date": row[1]}
        except Exception as e:
            logger.debug("get_sign_info(%s) 失败: %s", sid, e)
        return {"sign_count": 0, "last_sign_date": None}

    def update_sign(self, sid: str | int, score: int, gold: int,
                    sign_count: int, sign_date) -> None:
        """更新签到数据"""
        try:
            self.ctx.db_execute(
                "INSERT INTO fun_user (sid, score, gold, sign_count, last_sign_date) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE score=VALUES(score), gold=VALUES(gold), "
                "sign_count=VALUES(sign_count), last_sign_date=VALUES(last_sign_date)",
                (str(sid), int(score), int(gold), int(sign_count), sign_date),
            )
        except Exception as e:
            logger.warning("update_sign(%s) 失败: %s", sid, e)

    def get_score(self, sid: str | int) -> int:
        """获取用户积分"""
        try:
            row = self.ctx.db_query_one(
                "SELECT score FROM fun_user WHERE sid=%s", (str(sid),)
            )
            if row:
                if isinstance(row, dict):
                    return int(row.get("score") or 0)
                return int(row[0] or 0)
        except Exception as e:
            logger.debug("get_score(%s) 失败: %s", sid, e)
        return 0

    def get_gold(self, sid: str | int) -> int:
        """获取用户金币"""
        try:
            row = self.ctx.db_query_one(
                "SELECT gold FROM fun_user WHERE sid=%s", (str(sid),)
            )
            if row:
                if isinstance(row, dict):
                    return int(row.get("gold") or 0)
                return int(row[0] or 0)
        except Exception as e:
            logger.debug("get_gold(%s) 失败: %s", sid, e)
        return 0

    # ===================== 排行榜 =====================

    def get_score_rank(self, top_n: int = 10) -> list[dict]:
        """获取积分排行榜（前 N 名）"""
        try:
            rows = self.ctx.db_query(
                "SELECT sid AS uid, score, uid AS nickname "
                "FROM fun_user WHERE score>0 ORDER BY score DESC LIMIT %s",
                (int(top_n),),
            )
            return [
                {"uid": r.get("uid", r[0] if isinstance(r, (list, tuple)) else ""),
                 "score": int(r.get("score", r[1] if isinstance(r, (list, tuple)) else 0)),
                 "nickname": r.get("nickname", r[2] if isinstance(r, (list, tuple)) else "")}
                if isinstance(r, dict)
                else {"uid": r[0], "score": int(r[1] or 0), "nickname": r[2]}
                for r in rows
            ]
        except Exception as e:
            logger.debug("get_score_rank 失败: %s", e)
        return []

    def get_today_sign_rank(self, sid: str | int) -> dict:
        """获取今日签到排名：总签到人数 + 当前用户排名"""
        today = datetime.date.today()
        total = 0
        rank = 0
        try:
            # 今日签到总人数
            row = self.ctx.db_query_one(
                "SELECT COUNT(*) AS c FROM fun_user WHERE last_sign_date = %s",
                (today,),
            )
            total = int(row.get("c", row[0] if isinstance(row, (list, tuple)) else 0)) if row else 0
        except Exception as e:
            logger.debug("get_today_sign_rank total 失败: %s", e)

        try:
            # 当前用户排名（按 sign_count 排序，先签到的排名靠前）
            row = self.ctx.db_query_one(
                "SELECT COUNT(*) + 1 AS r FROM fun_user "
                "WHERE last_sign_date = %s AND sign_count <= "
                "(SELECT sign_count FROM fun_user WHERE sid=%s AND last_sign_date=%s)",
                (today, str(sid), today),
            )
            rank = int(row.get("r", row[0] if isinstance(row, (list, tuple)) else total)) if row else total
        except Exception as e:
            logger.debug("get_today_sign_rank rank 失败: %s", e)

        return {"total": total, "rank": rank}
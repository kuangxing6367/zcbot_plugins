"""MySQL 数据库操作模块 - 白名单记录管理（同步 pymysql 版）"""

import logging

import pymysql
from pymysql.cursors import DictCursor

logger = logging.getLogger("zgric")


class DatabaseManager:
    """白名单数据库管理器（同步，每次请求新建连接）"""

    def __init__(self, host: str, port: int, user: str, password: str, db: str):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.db = db

    def _get_conn(self):
        """获取一个新连接（每次请求独立连接，避免连接池兼容问题）"""
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.db,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=DictCursor,
        )

    def ensure_tables(self):
        """确保白名单相关表存在"""
        sqls = [
            """
            CREATE TABLE IF NOT EXISTS whitelist_records (
                id INT AUTO_INCREMENT PRIMARY KEY,
                game_name VARCHAR(100) NOT NULL COMMENT '游戏名',
                user_sid VARCHAR(20) NOT NULL COMMENT '用户QQ',
                created_at DATETIME DEFAULT NULL COMMENT '添加时间',
                UNIQUE KEY uk_user (user_sid)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='MC白名单记录'
            """,
            """
            CREATE TABLE IF NOT EXISTS whitelist_attempts (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_sid VARCHAR(20) NOT NULL COMMENT '用户QQ',
                game_name VARCHAR(100) NOT NULL COMMENT '尝试添加的游戏名',
                created_at DATETIME DEFAULT NULL COMMENT '尝试时间',
                INDEX idx_user (user_sid),
                INDEX idx_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='白名单重复尝试记录'
            """,
            """
            CREATE TABLE IF NOT EXISTS ban_records (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_sid VARCHAR(20) NOT NULL COMMENT '用户QQ',
                ban_until DATETIME NOT NULL COMMENT '解禁时间',
                created_at DATETIME DEFAULT NULL COMMENT '禁言时间',
                INDEX idx_user (user_sid),
                INDEX idx_ban (ban_until)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='白名单违规禁言记录'
            """,
        ]
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                for sql in sqls:
                    cur.execute(sql)
            logger.info("[MC-WHITELIST] 白名单表结构已就绪（独立数据库）")
        except Exception as e:
            logger.warning(f"[MC-WHITELIST] 创建白名单表失败: {e}")
        finally:
            conn.close()

    # ========== 白名单查询 ==========

    def is_whitelist_exists(self, user_sid: str) -> bool:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM whitelist_records WHERE user_sid = %s LIMIT 1",
                    (user_sid,),
                )
                return cur.fetchone() is not None
        finally:
            conn.close()

    def get_whitelist_game(self, user_sid: str) -> str | None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT game_name FROM whitelist_records WHERE user_sid = %s LIMIT 1",
                    (user_sid,),
                )
                row = cur.fetchone()
                return row["game_name"] if row else None
        finally:
            conn.close()

    def add_whitelist_record(self, user_sid: str, game_name: str) -> None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO whitelist_records (game_name, user_sid) VALUES (%s, %s)",
                    (game_name, user_sid),
                )
            logger.info(f"[MC-WHITELIST] 已记录白名单: user={user_sid}, game={game_name}")
        except Exception as e:
            logger.error(f"[MC-WHITELIST] 记录白名单失败: {e}")
        finally:
            conn.close()

    def upsert_whitelist_record(self, user_sid: str, game_name: str) -> None:
        """写入/覆盖白名单记录（强制注册用，冲突时覆盖 game_name）"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO whitelist_records (game_name, user_sid) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE game_name = VALUES(game_name)",
                    (game_name, user_sid),
                )
            logger.info(f"[MC-WHITELIST] 强制注册已写入白名单: user={user_sid}, game={game_name}")
        except Exception as e:
            logger.error(f"[MC-WHITELIST] 强制注册写入白名单失败: {e}")
        finally:
            conn.close()

    # ========== 尝试记录 ==========

    def record_attempt(self, user_sid: str, game_name: str) -> None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO whitelist_attempts (user_sid, game_name) VALUES (%s, %s)",
                    (user_sid, game_name),
                )
        finally:
            conn.close()

    def count_recent_attempts(self, user_sid: str) -> int:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM whitelist_attempts "
                    "WHERE user_sid = %s AND created_at >= NOW() - INTERVAL 1 DAY",
                    (user_sid,),
                )
                row = cur.fetchone()
                return row["cnt"] if row else 0
        finally:
            conn.close()

    # ========== 禁言记录 ==========

    def check_ban(self, user_sid: str) -> bool:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM ban_records WHERE user_sid = %s AND ban_until > NOW() LIMIT 1",
                    (user_sid,),
                )
                return cur.fetchone() is not None
        finally:
            conn.close()

    def get_ban_until(self, user_sid: str) -> str | None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ban_until FROM ban_records WHERE user_sid = %s "
                    "AND ban_until > NOW() ORDER BY ban_until DESC LIMIT 1",
                    (user_sid,),
                )
                row = cur.fetchone()
                return str(row["ban_until"]) if row else None
        finally:
            conn.close()

    def add_ban(self, user_sid: str, duration_hours: int = 24) -> None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ban_records (user_sid, ban_until) VALUES (%s, NOW() + INTERVAL %s HOUR)",
                    (user_sid, duration_hours),
                )
            logger.info(f"[MC-WHITELIST] 用户 {user_sid} 已被禁言 {duration_hours} 小时")
        finally:
            conn.close()
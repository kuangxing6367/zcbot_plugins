"""
违禁词检测模块（zgric_onebot11 框架适配版）
============================================

功能：
1. 本地自定义违禁词检测（打码）
2. 远程违禁词 API 检测（替代内置禁词库）
3. 命中后统一处理：撤回原消息 + 禁言发送者 + 发送打码后的文本提示

适配说明：
- 原版 aiohttp 异步请求改为 requests 同步
- 原版 db 连接池改为 ctx.db_query()/ctx.db_execute()
- 原版 bot.xxx 直接 API 调用改为 ctx.api()/ctx.ban()
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger("fun_score")


class BanWordHandle:
    """违禁词检测处理器"""

    def __init__(self, ctx):
        """
        :param ctx: 插件上下文（由 register() 注入）
        """
        self.ctx = ctx
        bw_cfg = ctx.get_config("ban_word", {})
        if not isinstance(bw_cfg, dict):
            bw_cfg = {}
        self.enabled = bw_cfg.get("enabled", True)
        self.api_url = bw_cfg.get("api_url", "https://api-v2.yuafeng.cn/API/wjc.php")
        self.ban_time = int(bw_cfg.get("ban_time", 360))
        self.timeout = int(bw_cfg.get("timeout", 3))

    # ===================== 本地违禁词检测 =====================

    def _get_local_ban_words(self) -> list[str]:
        """从配置读取本地违禁词列表（逗号分隔）"""
        raw = self.ctx.get_config("ban_word_list", "") or ""
        return [w.strip() for w in raw.split(",") if w.strip()]

    def _find_ban_words(self, text: str, words: list[str]) -> list[str]:
        """返回命中的违禁词列表（去重）"""
        hit = []
        for w in words:
            if w and w in text and w not in hit:
                hit.append(w)
        return hit

    def _mask_text(self, text: str, words: list[str]) -> str:
        """把命中违禁词的部分替换为 **"""
        masked = text
        for w in words:
            if w:
                masked = masked.replace(w, "**")
        return masked

    # ===================== 远程 API 检测 =====================

    def check_ban_word_api(self, text: str) -> bool:
        """调用违禁词 API 检测文本，命中返回 True"""
        if not self.enabled:
            return False
        try:
            resp = requests.get(
                self.api_url,
                params={"text": text},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            # 兼容多种返回格式
            if isinstance(data, dict):
                if data.get("code") in (1, 200, "1", "200") and data.get("hit"):
                    return True
                if str(data.get("status", "")).lower() in ("hit", "true", "1"):
                    return True
                if data.get("contains"):
                    return True
                words = data.get("words") or data.get("data")
                if isinstance(words, list) and words:
                    return True
            elif isinstance(data, str):
                return "hit" in data.lower() or "违禁" in data
        except Exception as e:
            logger.debug("违禁词API请求失败: %s", e)
        return False

    def check_ban_word_api_words(self, text: str) -> list[str]:
        """调用违禁词 API 检测文本，返回命中的违禁词列表（用于打码）"""
        if not self.enabled:
            return []
        try:
            resp = requests.get(
                self.api_url,
                params={"text": text},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                words = data.get("words") or data.get("data") or []
                if isinstance(words, list):
                    return [str(w) for w in words if w]
                if data.get("hit") or data.get("contains"):
                    # API 命中但未返回具体词，返回空列表，由调用方用 ** 全文打码兜底
                    return []
            elif isinstance(data, str):
                if "hit" in data.lower() or "违禁" in data:
                    return []
        except Exception as e:
            logger.debug("违禁词API请求失败: %s", e)
        return []

    # ===================== 处理流程 =====================

    def _get_user_nickname(self, uid: int) -> str:
        """从数据库获取用户昵称"""
        try:
            row = self.ctx.db_query_one(
                "SELECT uid FROM fun_user WHERE sid = %s", (str(uid),)
            )
            if row:
                return str(row.get("uid", str(uid)) if isinstance(row, dict) else row[0])
        except Exception:
            pass
        return str(uid)

    def _handle_ban_word_hit(self, event, masked_text: str) -> None:
        """
        违禁词命中统一处理：撤回原消息 + 禁言 + 发送打码后的文本提示。
        """
        gid = event.group_id
        uid = event.user_id

        # 1. 撤回原消息
        message_id = getattr(event, "message_id", None)
        if message_id is not None:
            try:
                self.ctx.api("delete_msg", message_id=int(message_id))
            except Exception:
                pass

        # 2. 禁言发送者
        if self.ban_time > 0 and gid and uid:
            try:
                self.ctx.ban(int(gid), int(uid), self.ban_time)
            except Exception:
                logger.error("bot在群%s权限不足，禁言失败", gid)

        # 3. 发送打码后的文本提示
        nickname = self._get_user_nickname(int(uid))
        tip = f"{nickname}的消息包含违禁词，已打码处理"
        self.ctx.send_msg(
            user_id=uid,
            group_id=gid,
            message=f"{tip}\n打码后：{masked_text}",
        )

    def on_ban_words(self, event) -> None:
        """检测违禁词：打码 + 撤回 + 禁言

        优先级：API 远程检测 > 本地自定义违禁词列表
        """
        text = getattr(event, "message", "") or getattr(event, "message_str", "") or ""

        # 1. 远程 API 检测（优先）
        if self.enabled:
            api_words = self.check_ban_word_api_words(text)
            if api_words:
                masked = self._mask_text(text, api_words)
                self._handle_ban_word_hit(event, masked)
                return
            # API 命中但未返回具体词，用 ** 全文打码兜底
            if self.check_ban_word_api(text):
                masked = "**" * 6
                self._handle_ban_word_hit(event, masked)
                return

        # 2. 本地违禁词列表检测
        local_words = self._get_local_ban_words()
        if local_words:
            hit_words = self._find_ban_words(text, local_words)
            if hit_words:
                masked = self._mask_text(text, hit_words)
                self._handle_ban_word_hit(event, masked)
                return
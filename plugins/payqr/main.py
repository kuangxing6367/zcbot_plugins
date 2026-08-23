"""
收款码插件 (payqr)
====================
功能（从 AstrBot「payqr」类插件迁移，功能逻辑重写）：
  /收款 <金额> [备注]  -> 生成包含收款信息的二维码图片并返回
  /收款帮助           -> 说明
二维码内容由插件配置 payee_name / payee_account 拼接，经公共 QR 服务生成图片。
"""
import re
from urllib.parse import quote

import httpx

__plugin_meta__ = {
    "name": "收款码",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "生成收款信息二维码（可配置收款方）",
    "priority": 50,
}

ctx = None

_QR_API = "https://api.qrserver.com/v1/create-qr-code/"
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _cfg(key, default):
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


async def _reply(event, msg):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )


async def handle_pay(event, match):
    """收款 <金额> [备注]"""
    text = (match.group(1) if match else "").strip()
    m = re.match(r"(\d+(?:\.\d+)?)\s*([\s\S]*)", text)
    if not m:
        await _reply(event, "用法: /收款 <金额> [备注]，如 /收款 10 饭钱")
        return
    amount = m.group(1)
    note = m.group(2).strip()
    payee = _cfg("payee_name", "") or _cfg("payee_account", "收款方")
    content = f"收款方：{payee}\n金额：{amount}\n备注：{note or '无'}"
    url = f"{_QR_API}?size=300x300&data={quote(content)}"
    try:
        async with httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            await ctx.asend_msg(
                user_id=event.user_id,
                group_id=event.group_id if event.is_group else None,
                message=f"[CQ:image,file={str(r.url)}]",
            )
    except Exception as e:
        await _reply(event, f"生成收款码失败: {e}")


async def handle_help(event, match):
    await _reply(event, "💰 收款码\n▸ /收款 <金额> [备注]  → 生成收款二维码\n▸ /收款帮助\n（可在插件配置填写 payee_name / payee_account）")


def register(ctx_obj):
    global ctx
    ctx = ctx_obj
    ctx.command("/收款\\s+([\\s\\S]+)", handle_pay, priority=50, description="生成收款二维码")
    ctx.command("/收款帮助\\s*$", handle_help, priority=50, description="收款码帮助")
    ctx.logger.info("收款码插件注册完成")

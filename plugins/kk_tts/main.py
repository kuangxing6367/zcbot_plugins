"""
恶搞语音 TTS 插件（枫雨 kktts 接口）
将文字通过枫雨「恶搞语音文字转语音」接口转换为抽象语音音频（mp3），
再转码为 QQ 官方支持的 Tencent Silk 格式后作为语音消息发送。
同时注册为 LLM AI 函数，允许 LLM 在对话中调用本插件生成语音。

协议说明：
- 入站 mp3 来自 https://api-v2.yuafeng.cn/API/kktts.php?content=..&action=voice&voice_id=..
- QQ 官方机器人发送语音必须为 Tencent Silk（silk_v3 + tencent 封装），
- 本机无 ffmpeg，mp3 解码用 miniaudio（自带 dr_mp3），silk 编码用 pysilk(tencent=True)
"""
import asyncio
import io
import os
import re
import tempfile
import time

import requests

__plugin_meta__ = {
    "name": "恶搞语音TTS",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "调用枫雨恶搞语音TTS，文字转抽象语音并转码Silk后发送；已注册为LLM AI函数",
    "priority": 50,
}

_ctx = None
_llm_registered = False

DEFAULT_API = "https://api-v2.yuafeng.cn/API/kktts.php"
DEFAULT_VOICE_ID = "94b8f3ec59b18723224b7ac5e3fa3a07"


def register(ctx):
    global _ctx
    _ctx = ctx
    ctx.command("/恶搞语音", handle_tts,
                alias=["/kktts", "/抽象语音", "/恶搞念"],
                description="将文字转换为恶搞/抽象语音发送（用法: /恶搞语音 文字）")
    # 注册 LLM 函数（llm_core 可能晚于本插件加载，故同时监听加载事件补注册）
    _try_register_llm()
    ctx.on("system.plugin.loaded", _on_plugins_loaded)


def _on_plugins_loaded(payload):
    _try_register_llm()


# ============ 命令处理 ============

async def handle_tts(event, match):
    text = (match.group(1) if match else "") or (event.message or "").strip()
    # 支持 "vid:xxx 文字" 指定声音
    voice_id = None
    m = re.match(r"^vid[:：]\s*(\S+)\s+(.*)$", text, re.S)
    if m:
        voice_id = m.group(1).strip()
        text = m.group(2).strip()
    if not text:
        _ctx.send_msg(user_id=event.user_id,
                      group_id=event.group_id if event.is_group else None,
                      message="用法: /恶搞语音 <文字>  （可选 vid:<声音ID> 指定声音）")
        return
    await tts_and_send(text, event.user_id,
                      event.group_id if event.is_group else None, voice_id)


# ============ 核心：TTS + 转码 + 发送 ============

async def _do_tts(text, user_id, group_id, voice_id=None):
    if _ctx is None:
        return "插件未就绪"
    text = (text or "").strip()
    if not text:
        return "错误：文字为空"
    max_len = int(_ctx.get_config("max_text_len", 200) or 200)
    if len(text) > max_len:
        text = text[:max_len]
        _ctx.log(f"[kk_tts] 文字超长已截断至 {max_len} 字", level="warning")

    voice_id = voice_id or _ctx.get_config("voice_id", "") or DEFAULT_VOICE_ID
    api = _ctx.get_config("api_url", DEFAULT_API) or DEFAULT_API

    # 1. 调枫雨接口
    try:
        resp = await asyncio.to_thread(
            requests.get, api,
            params={"content": text, "action": "voice", "voice_id": voice_id},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _ctx.log(f"[kk_tts] 调用枫雨接口失败: {e}", level="error")
        return f"调用枫雨TTS接口失败: {e}"

    if int(data.get("code", -1)) != 0:
        return f"枫雨TTS返回错误: {data.get('msg')} (code={data.get('code')})"
    mp3_url = (data.get("data") or {}).get("url")
    if not mp3_url:
        return "枫雨TTS未返回音频URL"

    # 2. 下载 mp3 + 转 silk（CPU/IO 密集，丢线程池）
    tmp_dir = tempfile.mkdtemp(prefix="kktts_")
    mp3_path = os.path.join(tmp_dir, "src.mp3")
    silk_path = os.path.join(tmp_dir, "out.silk")
    try:
        await asyncio.to_thread(_download, mp3_url, mp3_path)
        silk = await asyncio.to_thread(_mp3_to_tencent_silk, mp3_path)
        with open(silk_path, "wb") as f:
            f.write(silk)
    except Exception as e:
        _ctx.log(f"[kk_tts] 下载/转码失败: {e}", level="error")
        return f"语音转换失败（需环境安装 miniaudio+pysilk）: {e}"

    # 3. 发送语音（record 段，qq_official_bot 适配器会转 base64 上传为 voice）
    try:
        abs_path = os.path.abspath(silk_path)
        await _ctx.asend_msg(
            user_id=user_id,
            group_id=group_id if group_id else None,
            message=f"[CQ:record,file=file:///{abs_path}]",
        )
    except Exception as e:
        _ctx.log(f"[kk_tts] 发送语音失败: {e}", level="error")
        return f"语音发送失败: {e}"

    # 延时清理临时文件（确保 OneBot 客户端读取完成）
    _ctx.run_async(_delayed_remove, tmp_dir, 180)
    return "已发送恶搞语音 🎤"


# tts_and_send 别名（命令与 LLM 共用入口）
tts_and_send = _do_tts


def _download(url, path):
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)


def _mp3_to_tencent_silk(mp3_path, sample_rate=24000):
    """mp3 -> PCM(16k mono s16) -> Tencent Silk。返回 silk bytes。

    """
    try:
        import miniaudio
    except ImportError as e:
        raise RuntimeError(f"缺少 miniaudio（mp3 解码库），请安装: pip install miniaudio ({e})")

    try:
        dsf = miniaudio.decode_file(
            mp3_path,
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=sample_rate,
        )
        pcm = dsf.samples
        sr = dsf.sample_rate
    except Exception as e:
        raise RuntimeError(f"mp3 解码失败: {e}")
    if not pcm:
        raise RuntimeError("mp3 解码为空")

    silk = None
    err_pysilk = None
    err_pilk = None

    # 后端1: pysilk（由 silk-python 提供，自带 tencent=True，有预编译 wheel，无需 gcc）
    try:
        import pysilk
        out = io.BytesIO()
        pysilk.encode(io.BytesIO(pcm), out, sr, 24000, tencent=True)
        silk = out.getvalue()
    except Exception as e:
        err_pysilk = e

    if silk is None:
        try:
            import pilk
            import wave
            wav_tmp = mp3_path + ".wav"
            with wave.open(wav_tmp, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes(pcm)
            silk_tmp = mp3_path + ".silk"
            pilk.encode(wav_tmp, silk_tmp, pcm_rate=sr, tencent=True)
            with open(silk_tmp, "rb") as f:
                silk = f.read()
            try:
                os.remove(wav_tmp)
                os.remove(silk_tmp)
            except Exception:
                pass
        except Exception as e:
            err_pilk = e

    if not silk:
        raise RuntimeError(
            "silk 编码失败（pysilk/pilk 均不可用）。请在框架 Python 环境安装: "
            f"pip install silk-python（提供 pysilk 模块，含预编译 wheel）。pysilk错误={err_pysilk}; pilk错误={err_pilk}"
        )
    return silk


def _delayed_remove(path, delay):
    """线程里延时删除临时目录（run_async 在线程池执行）"""
    import shutil
    time.sleep(delay)
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


# ============ LLM AI 函数 ============

def _try_register_llm():
    global _llm_registered
    if _llm_registered:
        return
    if _ctx is None:
        return
    if not _ctx.get_config("enable_llm", True):
        return
    try:
        svc = _ctx._framework.services.get('llm_core')  # llm_core 服务门面
        if svc is None:
            raise RuntimeError("llm_core 服务未加载")

        @svc.tool(name="kk_tts",
                  description=("将文字转换为恶搞/抽象语音并直接发送语音消息。"
                               "当用户想用趣味、搞怪、抽象的声音念出某段文字，或明确要求生成语音时使用。"
                               "参数 text 为要念出的文字；voice_id 可选，指定不同恶搞声音。"))
        def kk_tts(text: str, voice_id: str = '', event=None):
            return _fn_kk_tts({'text': text, 'voice_id': voice_id}, None, event, None)
        _llm_registered = True
        _ctx.log("[kk_tts] LLM 函数 kk_tts 注册成功", level="info")
    except Exception as e:
        _ctx.log(f"[kk_tts] LLM 函数注册失败（llm_core 未加载?）: {e}", level="warning")


async def _fn_kk_tts(args, _c, _event, _user_id):
    try:
        text = str(args.get("text") or "").strip()
    except Exception:
        text = ""
    if not text:
        return "错误：缺少 text 参数（要转换成语音的文字）"
    voice_id = args.get("voice_id")
    if voice_id:
        voice_id = str(voice_id).strip() or None
    gid = _event.group_id if _event and getattr(_event, "is_group", False) else None
    uid = _event.user_id if _event else _user_id
    result = await tts_and_send(text, uid, gid, voice_id)
    return result

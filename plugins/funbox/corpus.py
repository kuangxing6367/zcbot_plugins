# -*- coding: utf-8 -*-
"""本地语料加载模块
=================
从插件目录 data/ 下的 JSON 文件读取本地语料（只读文件随插件分发，
以 __file__ 相对定位，不依赖运行目录）。带进程内缓存；
文件缺失或解析失败时回退内置最小兜底语料，保证各命令始终可用。
"""
import json
import os
import threading

# 语料目录：插件目录下的 data/
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

_CACHE = {}
_LOCK = threading.Lock()
_LOG = None  # 由插件入口注入日志函数（corpus 模块自身不持有 ctx）

# 内置最小兜底语料（仅当 data/ 文件异常缺失时启用，正常不会用到）
_FALLBACK = {
    "jokes.json": ["今天也要开心呀。"],
    "tongue_twisters.json": ["吃葡萄不吐葡萄皮，不吃葡萄倒吐葡萄皮。"],
    "brain_teasers.json": [{"q": "什么东西越洗越脏？", "a": "水"}],
    "fortune.json": {
        "levels": ["大吉", "中吉", "小吉", "吉", "半吉", "末吉", "凶", "大凶"],
        "constellations": ["白羊", "金牛", "双子", "巨蟹", "狮子", "处女",
                           "天秤", "天蝎", "射手", "摩羯", "水瓶", "双鱼"],
        "luck_words": ["宜学习，忌拖延。"],
    },
    "quotes.json": ["千里之行，始于足下。"],
    "quiz.json": {
        "questions": [{"q": "今天心情如何？",
                       "opts": ["A 很好", "B 还行", "C 一般", "D 不说"],
                       "score": {"A": 1, "B": 2, "C": 3, "D": 4}}],
        "results": [{"min": 1, "max": 4, "text": "🦌 神秘之鹿"}],
    },
}


def set_logger(log_fn):
    """注入日志函数（插件注册时调用），供语料加载异常时输出告警"""
    global _LOG
    _LOG = log_fn


def _load(name):
    """按文件名加载语料（带缓存）；失败回退内置兜底，绝不抛出"""
    with _LOCK:
        if name in _CACHE:
            return _CACHE[name]
        data = _FALLBACK.get(name)
        path = os.path.join(_DATA_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if loaded is not None:
                data = loaded
        except Exception as e:
            if _LOG:
                try:
                    _LOG(f"语料 {name} 加载失败，已回退内置兜底语料: {e}", level="warning")
                except Exception:
                    pass
        _CACHE[name] = data
        return data


# ---------------- 分类访问入口 ----------------

def jokes():
    """本地笑话语料（字符串列表）"""
    return _load("jokes.json") or []


def tongue_twisters():
    """绕口令语料（字符串列表）"""
    return _load("tongue_twisters.json") or []


def brain_teasers():
    """脑筋急转弯语料（[{q, a}] 列表）"""
    return _load("brain_teasers.json") or []


def quotes():
    """名言金句语料（字符串列表）"""
    return _load("quotes.json") or []


def fortune_levels():
    """签文/运势等级列表"""
    return (_load("fortune.json") or {}).get("levels") or ["吉"]


def constellations():
    """星座名列表（用于 /星座 参数匹配）"""
    return (_load("fortune.json") or {}).get("constellations") or []


def luck_words():
    """今日幸运语列表"""
    return (_load("fortune.json") or {}).get("luck_words") or ["诸事顺遂。"]


def quiz_questions():
    """测试题库（[{q, opts, score}] 列表）"""
    return (_load("quiz.json") or {}).get("questions") or []


def quiz_results():
    """测试结果称号（[{min, max, text}] 列表，按累计得分区间映射）"""
    return (_load("quiz.json") or {}).get("results") or []

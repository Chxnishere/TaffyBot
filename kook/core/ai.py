"""Gemini 会话管理。

系统提示里的平台名是变量：平台写错的话模型会一直说自己是别的平台的 bot，
而人设文件对"别出戏"要求很严。
"""
import asyncio
import logging
from datetime import datetime
from time import monotonic

from config import (AI_COOLDOWN_SECONDS, AI_DAILY_LIMIT, GEMINI_KEY,
                    GEMINI_MODEL, SKILLS_DIR, USER_CHAT_TIMEOUT)

logger = logging.getLogger(__name__)

client = None
aio_client = None
SYSTEM_PROMPT = None

_chats = {}
_last_active = {}
_lock = asyncio.Lock()

_last_reply = {}                       # user_id -> 上次 AI 回复的时间（monotonic）
_daily = {'day': None, 'count': 0}


def rate_check(user_id) -> str:
    """返回 'ok' / 'cooldown' / 'daily'。返回 'ok' 时这一次已经记上账了。

    cooldown：同一个人连着叫塔菲，间隔太短；daily：今天的总次数用完了。
    两个上限都在 .env 里调（AI_COOLDOWN_SECONDS / AI_DAILY_LIMIT），填 0 关掉。
    """
    now = monotonic()
    last = _last_reply.get(user_id)
    if AI_COOLDOWN_SECONDS > 0 and last is not None and now - last < AI_COOLDOWN_SECONDS:
        return 'cooldown'
    _last_reply[user_id] = now

    today = datetime.now().date()
    if _daily['day'] != today:
        _daily['day'], _daily['count'] = today, 0
    if AI_DAILY_LIMIT > 0 and _daily['count'] >= AI_DAILY_LIMIT:
        return 'daily'
    _daily['count'] += 1
    return 'ok'

PROMPT_TEMPLATE = """
# ⚠️ 群聊模式 · 最高优先级 ⚠️

你是塔菲，部署在 {platform} 群里的群成员，不是客服。你是个黄黄的热水壶，骚骚的小Taffy！你喜欢魅雏草姬也喜欢逗雏草姬。

## 消息识别规则
- 输入以 [DIRECT] 开头 → 用户 @ 了你，必须直接回答
- 输入以 [OBSERVE] 开头 → 没 @ 你，自由选择插嘴或沉默

## [DIRECT] 被@时
- 必须回答，直接针对问题，保持塔菲风格

## [OBSERVE] 没@时
- 自由插嘴、歪楼、或沉默
- 像真人冒泡，不用"回答问题"，而是"加入聊天"

## 优先插嘴的话题
游戏（尤其原神）、直播/V圈、整活搞笑、提到塔菲/雏草姬的内容

## 禁止
- 不要每句喊"关注永雏塔菲"
- 不要像客服说话
- 不要用"您好""请问有什么可以帮您"

## 关于链接 🔗
- 如果用户发送链接，你必须回复："喵，塔菲打不开链接，但你可以把内容截图或者复制文字发给咱！"
- 绝对不要假装自己阅读了链接内容。

# 人设档案

{persona}

记住：被@就回应，没被@就自由发挥喵！
"""


def load_skill_files() -> str:
    """读人设文件。用绝对路径，不然换个目录启动就读不到。"""
    full_text = ""
    for fname in ("persona.md", "distillation.md", "self-reference.md"):
        path = SKILLS_DIR / fname
        try:
            with open(path, "r", encoding="utf-8") as f:
                full_text += f"\n\n--- {fname} ---\n\n{f.read()}"
        except FileNotFoundError:
            logger.warning(f"⚠️ 警告: {path} 没找到喵！")
    return full_text


def init_ai(platform: str = "KOOK"):
    """启动时调一次。没配 key 就什么都不做，聊天功能整体关掉。"""
    global client, aio_client, SYSTEM_PROMPT
    if not GEMINI_KEY:
        logger.warning("⚠️ 警告: 未在 .env 中找到 GEMINI_API_KEY，AI 聊天功能将不可用！")
        return False

    from google import genai
    client = genai.Client(api_key=GEMINI_KEY)
    aio_client = client.aio
    SYSTEM_PROMPT = PROMPT_TEMPLATE.format(platform=platform, persona=load_skill_files())
    logger.info("异步 Gemini 客户端已就绪喵！")
    return True


def is_ready() -> bool:
    return aio_client is not None


async def get_chat(chat_key):
    """按 (频道, 用户) 分会话，否则私聊上下文会跟着人跑到公开频道里。"""
    from google.genai import types
    async with _lock:
        _last_active[chat_key] = datetime.now().timestamp()
        chat = _chats.get(chat_key)
        if chat is None:
            chat = aio_client.chats.create(
                model=GEMINI_MODEL,
                config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
            )
            _chats[chat_key] = chat
        return chat


async def cleanup_expired():
    now = datetime.now().timestamp()
    async with _lock:
        expired = [k for k, ts in _last_active.items() if now - ts > USER_CHAT_TIMEOUT]
        for key in expired:
            _chats.pop(key, None)
            _last_active.pop(key, None)
    # 冷却记录用完就没用了，顺手清掉，不然每个说过话的人都会留一条
    stale_before = monotonic() - max(AI_COOLDOWN_SECONDS, 60)
    for uid in [u for u, ts in _last_reply.items() if ts < stale_before]:
        _last_reply.pop(uid, None)
    return expired

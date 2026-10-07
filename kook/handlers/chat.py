"""AI 聊天：关键词触发、被 @ 回复、随机插嘴、私聊。

对应 Discord 版的 on_message。主要差别：

1. 提及：Discord 是 `bot.user in message.mentions` + 把 `<@id>` 替换掉；
   KOOK 是 msg.extra['mention'] 里的 id 列表 + `(met)id(met)` 正则。
2. 私聊：KOOK 的私信是独立的 PrivateMessage，不是 channel 的一个变体，
   所以两个入口共用同一个 _respond。
3. 没有 typing 指示器。人设文件里本来就写了"先丢一句很短的过程提示"，
   这里就用一条占位消息代替，在中国网络下等 Gemini 时也更友好。
4. 长度上限 1900 是 Discord 的数字，换成 config.MAX_REPLY_CHARS。
"""
import logging
import random
import re

import aiohttp
from khl import Bot, Message, MessageTypes, PrivateMessage, PublicMessage

from config import (INTERJECT_CHANCE, MAX_IMAGE_MB, MAX_IMAGES_PER_MESSAGE,
                    MAX_REPLY_CHARS)
from core import ai
from handlers.common import safe_reply, truncate

logger = logging.getLogger(__name__)

_bot_id = None
_mention_re = None


def set_bot_identity(user_id: str):
    global _bot_id, _mention_re
    _bot_id = str(user_id)
    _mention_re = re.compile(rf'\(met\){re.escape(_bot_id)}\(met\)')


def _strip_mention(content: str) -> str:
    if _mention_re is None:
        return content.strip()
    return _mention_re.sub('', content).strip()


async def _download(url: str) -> bytes:
    """下载图片。图片是整个读进内存再转给 AI 的，所以超过 MAX_IMAGE_MB 就不要了。"""
    limit = int(MAX_IMAGE_MB * 1024 * 1024) if MAX_IMAGE_MB > 0 else 0
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise ValueError("下载附件失败")
            if not limit:
                return await resp.read()
            if (resp.content_length or 0) > limit:
                raise ValueError(f"图片超过 {MAX_IMAGE_MB:g}MB，跳过")
            # 服务器不一定老实报大小，所以读的时候也只读到上限为止
            data = await resp.content.read(limit + 1)
            while len(data) <= limit:
                more = await resp.content.read(limit + 1 - len(data))
                if not more:
                    return data
                data += more
            raise ValueError(f"图片超过 {MAX_IMAGE_MB:g}MB，跳过")


def _image_attachments(msg) -> list:
    """KOOK 的图片有两种形态：整条消息 type=IMAGE，或 extra.attachments。

    content_type 也比 Discord 粗（只有 image/file/video），所以还看扩展名兜底。
    """
    out = []
    extra = getattr(msg, 'extra', {}) or {}

    if msg.type == MessageTypes.IMG and msg.content.startswith('http'):
        out.append((msg.content, 'image/png'))

    att = extra.get('attachments')
    items = att if isinstance(att, list) else ([att] if isinstance(att, dict) else [])
    for item in items:
        url = item.get('url')
        if not url:
            continue
        kind = (item.get('type') or '').lower()
        name = (item.get('name') or url).lower()
        if kind.startswith('image') or name.endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif')):
            mime = 'image/png'
            if name.endswith(('.jpg', '.jpeg')):
                mime = 'image/jpeg'
            elif name.endswith('.webp'):
                mime = 'image/webp'
            elif name.endswith('.gif'):
                mime = 'image/gif'
            out.append((url, mime))
    return out


async def _respond(bot, msg, is_mentioned: bool):
    from google.genai import types

    if not ai.is_ready():
        await msg.reply("塔菲的大脑还没装好喵！")
        return

    user_prompt = _strip_mention(msg.content)
    if is_mentioned and not user_prompt:
        await msg.reply("叫塔菲有什么事喵？(ฅ'ω'ฅ)")
        return

    verdict = ai.rate_check(str(msg.author_id))
    if verdict != 'ok':
        # 随机插嘴被限流就安静跳过；点名/私聊的给个反馈，不然像是坏了
        if is_mentioned or isinstance(msg, PrivateMessage):
            try:
                if verdict == 'cooldown':
                    await msg.add_reaction('⏳')
                else:
                    await msg.reply("塔菲今天的话说完了喵，明天再聊~")
            except Exception as e:
                logger.debug(f"发送限流提示失败: {e}")
        return

    prefix = "[DIRECT]" if is_mentioned else "[OBSERVE]"
    contents = [f"{prefix} {user_prompt}"]

    images = _image_attachments(msg)
    if MAX_IMAGES_PER_MESSAGE > 0:
        images = images[:MAX_IMAGES_PER_MESSAGE]
    for url, mime in images:
        try:
            data = await _download(url)
            contents.append(types.Part.from_bytes(data=data, mime_type=mime))
        except Exception as e:
            logger.error(f"下载图片失败: {e}")

    chat_key = (msg.target_id, msg.author_id)
    chat = await ai.get_chat(chat_key)

    # KOOK 没有 typing 指示器，用一条占位消息代替
    placeholder = None
    try:
        placeholder = await msg.reply("（想一下）你别急喵……", use_quote=False)
    except Exception:
        pass

    try:
        response = await chat.send_message(contents)
        reply_text = truncate((response.text or '').strip() or "……塔菲没话说了喵。", MAX_REPLY_CHARS)
    except Exception as e:
        logger.error(f"API报错: {e}")
        reply_text = "塔菲死机了喵！(▰˘◡˘▰)"

    msg_id = (placeholder or {}).get('msg_id') if isinstance(placeholder, dict) else None
    if msg_id:
        from khl import api
        # 私信的消息要走 direct-message 那一套接口，用频道的 message/update 改不动，
        # 以前私聊里占位消息永远改不掉，每次都是"你别急喵"+ 另一条回复。
        endpoint = api.DirectMessage if isinstance(msg, PrivateMessage) else api.Message
        try:
            await bot.client.gate.exec_req(endpoint.update(msg_id=msg_id, content=reply_text))
            return
        except Exception as e:
            logger.debug(f"改写占位消息失败，改为直接回复: {e}")
            # 改不了（多半是 AI 回复过不了 KMarkdown 校验）就把占位消息删掉，
            # 别让"你别急喵"一直挂在那。
            try:
                await bot.client.gate.exec_req(endpoint.delete(msg_id=msg_id))
            except Exception as e2:
                logger.debug(f"删除占位消息失败: {e2}")
    # Gemini 的输出里可能有落单的方括号或链接，KMarkdown 过不了校验，
    # safe_reply 会自动退回纯文本重发。
    await safe_reply(msg, reply_text)


def setup(bot: Bot):

    @bot.on_message()
    async def on_message(msg: Message):
        # 注意：这个类型标注是必需的，不是装饰。
        # khl.py 的 client.register 会校验
        # issubclass(params[0].annotation, RawMessage)，没标注直接抛 TypeError。
        try:
            if str(msg.author_id) == str(_bot_id):
                return

            # 命令交给 CommandManager，这里只要别让 /play 之类触发随机插嘴
            if msg.content.startswith('/'):
                return

            is_private = isinstance(msg, PrivateMessage)
            mentions = (getattr(msg, 'extra', {}) or {}).get('mention') or []
            is_mentioned = _bot_id in [str(m) for m in mentions]

            # 关键词触发，和 Discord 版一模一样
            if not is_mentioned:
                if "死" in msg.content:
                    await msg.reply("好似喵！(΄◞ิ౪◟ิ‵)")
                    return
                if "关注" in msg.content:
                    await msg.reply("关注塔菲喵~关注塔菲谢谢喵！(｡◕∀◕｡)")
                    return

            if is_mentioned or is_private:
                await _respond(bot, msg, is_mentioned)
                return

            if isinstance(msg, PublicMessage) and random.random() <= INTERJECT_CHANCE:
                await _respond(bot, msg, False)
        except Exception as e:
            logger.error(f"消息处理异常: {e}", exc_info=e)

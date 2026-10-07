"""/ping /help /dog"""
import json
import logging
import time

import aiohttp
from khl import Bot, Message, User

from config import DOG_API_KEY
from handlers.common import rest
from render import cards

logger = logging.getLogger(__name__)


def setup(bot: Bot):

    @bot.command(name='ping')
    async def ping(msg: Message):
        """KOOK 没有 Discord 那种现成的网关心跳延迟，自己打一次 API 测往返。"""
        start = time.monotonic()
        await bot.client.fetch_me()
        latency = round((time.monotonic() - start) * 1000)
        await msg.reply(f"当前延迟是 {latency}ms 喵！(ฅ'ω'ฅ)")

    @bot.command(name='help')
    async def help_command(msg: Message):
        avatar = None
        try:
            me = await bot.client.fetch_me()
            avatar = me.avatar
        except Exception:
            pass
        # Discord 版是 ephemeral，这里用 is_temp：只有发起者看得见
        await msg.reply(cards.help_card(avatar), is_temp=True)
        logger.info(f"用户 {msg.author.username} 使用了 /help")

    @bot.command(name='dog')
    async def dog(msg: Message, user: User = None, *rest_args):
        """参数标注成 User，khl.py 的 parser 会自动把 (met)id(met) 解析成 User 对象。"""
        if user is None:
            await msg.reply("要 @ 谁呀喵？用法：`/dog @某人`", is_temp=True)
            return

        api_url = f"https://api.oick.cn/api/dog?apikey={DOG_API_KEY}"
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(api_url) as resp:
                    if resp.status != 200:
                        await msg.reply("呜……舔狗 API 请求失败了喵！")
                        return
                    raw = await resp.text()
        except Exception as e:
            logger.error(f"/dog API 异常: {e}")
            await msg.reply("舔狗 API 暂时不可用喵，稍后再试试吧~")
            return

        text = ""
        try:
            data = json.loads(raw)
            text = data.get("content", "") if isinstance(data, dict) else str(data)
        except json.JSONDecodeError:
            text = raw

        text = text.strip()
        if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        if not text:
            text = "今天没怎么和你说话，我找了半个小时的文案，发了条朋友圈，仅你可见。"

        await msg.reply(f"(met){user.id}(met) {text}", use_quote=False)

"""/deal /hotdeals"""
import logging

import aiohttp
from khl import Bot, Message

from core.deals import (dedupe_best_per_title, filter_deals_with_fallback,
                        get_hot_deals, get_stores, search_deals)
from handlers.common import clean_query, reply_plain, rest
from render import cards

logger = logging.getLogger(__name__)


def setup(bot: Bot):

    @bot.command(name='deal')
    async def deal(msg: Message, *args):
        game = clean_query(rest(args))
        if not game:
            await msg.reply("要查哪个游戏呀喵？用法：`/deal Hades`", is_temp=True)
            return

        async with aiohttp.ClientSession() as session:
            await get_stores(session)
            deals = await search_deals(session, game, limit=5)

        if not deals:
            await msg.reply(f"呜……没找到 **{game}** 的折扣信息喵！换个游戏试试？")
            return
        for cm in cards.deal_cards(deals[:5]):
            await msg.reply(cm)

    @bot.command(name='hotdeals')
    async def hotdeals(msg: Message, *args):
        async with aiohttp.ClientSession() as session:
            await get_stores(session)
            deals = await get_hot_deals(session, page_size=60, pages=3)

        if not deals:
            await msg.reply("呜……热门折扣获取失败喵！稍后再试试吧~")
            return

        filtered = filter_deals_with_fallback(deals, min_results=5)
        if not filtered:
            await msg.reply("今天暂时没有符合条件的优质折扣喵！(´;ω;｀) 晚点再来看看吧~")
            return

        top = dedupe_best_per_title(filtered)[:10]
        # 10 个卡片超过一条消息 5 个的上限，要拆成多条发
        for cm in cards.deal_cards(top):
            await msg.reply(cm)

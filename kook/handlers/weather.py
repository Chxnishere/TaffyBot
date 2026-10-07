"""/sky"""
import logging

import aiohttp
from khl import Bot, Message

from core.weather import get_lat_lon, get_weather_by_coords
from handlers.common import rest
from render import cards

logger = logging.getLogger(__name__)


def setup(bot: Bot):

    @bot.command(name='sky')
    async def sky(msg: Message, *args):
        """Discord 版是 (city: str, country: str = "") 两个有类型的参数。

        KOOK 按空格切词，所以约定：最后一个 token 如果看着像国家代码
        （2 个字母），就当国家，其余都算城市名。
        """
        if not args:
            await msg.reply("要查哪个城市呀喵？用法：`/sky 北京` 或 `/sky Tokyo JP`", is_temp=True)
            return

        args = list(args)
        country = ""
        if len(args) > 1 and len(args[-1]) == 2 and args[-1].isalpha():
            country = args[-1].upper()
            args = args[:-1]
        city = rest(args)

        async with aiohttp.ClientSession() as session:
            lat, lon, zh_name, country_code = await get_lat_lon(session, city, country)
            if not lat or not lon:
                await msg.reply(f"呜……塔菲找不到城市 **{city}** 喵！请检查输入是否正确~")
                return

            w = await get_weather_by_coords(session, lat, lon)
            if not w or "main" not in w:
                await msg.reply("呜……天气服务器开小差了，获取失败喵！")
                return

        await msg.reply(cards.weather_card(
            zh_name,
            country_code,
            w["weather"][0]["description"],
            w["main"]["temp"],
            w["main"]["feels_like"],
            w["main"]["humidity"],
            w["wind"]["speed"],
        ))

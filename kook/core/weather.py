"""OpenWeatherMap 查询。和平台无关，从 Discord 版原样搬来。"""
import logging
import urllib.parse

import aiohttp

from config import OWM_KEY

logger = logging.getLogger(__name__)


async def get_lat_lon(session: aiohttp.ClientSession, city: str, country: str = ""):
    query = f"{city},{country}" if country else city
    encoded = urllib.parse.quote(query)
    url = (f"https://api.openweathermap.org/geo/1.0/direct"
           f"?q={encoded}&limit=1&appid={OWM_KEY}")
    try:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                if data:
                    local_names = data[0].get("local_names", {})
                    return (data[0]["lat"], data[0]["lon"],
                            local_names.get("zh", data[0]["name"]),
                            data[0].get("country", ""))
    except Exception as e:
        logger.error(f"Geocoding API 报错喵: {e}")
    return None, None, None, None


async def get_weather_by_coords(session: aiohttp.ClientSession, lat, lon):
    url = (f"https://api.openweathermap.org/data/2.5/weather"
           f"?lat={lat}&lon={lon}&appid={OWM_KEY}&units=metric&lang=zh_cn")
    try:
        async with session.get(url) as response:
            if response.status == 200:
                return await response.json()
    except Exception as e:
        logger.error(f"Weather API 报错喵: {e}")
    return None

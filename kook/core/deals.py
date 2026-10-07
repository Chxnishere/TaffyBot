"""CheapShark 折扣查询。和平台无关，从 Discord 版原样搬来。"""
import asyncio
import logging
import urllib.parse

import aiohttp

from core.db import kv_get, kv_set

logger = logging.getLogger(__name__)

UA = {'User-Agent': 'TaffyBot/1.0 (KOOK Bot)'}
stores_cache = {}


async def get_stores(session: aiohttp.ClientSession):
    global stores_cache
    if stores_cache:
        return stores_cache

    # 重启后先看数据库缓存，省一次 API 调用；店铺列表一周更一次够了
    try:
        cached = kv_get('cheapshark_stores', max_age_seconds=7 * 86400)
        if cached:
            stores_cache = cached
            return stores_cache
    except Exception as e:
        logger.error(f"读取店铺缓存失败: {e}")

    url = "https://www.cheapshark.com/api/1.0/stores"
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with session.get(url, headers=UA, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                stores_cache = {str(s['storeID']): s['storeName'] for s in data}
                try:
                    kv_set('cheapshark_stores', stores_cache)
                except Exception as e:
                    logger.error(f"写入店铺缓存失败: {e}")
            else:
                logger.error(f"获取商店列表失败，状态码: {resp.status}")
                stores_cache = {}
    except asyncio.TimeoutError:
        logger.error("获取商店列表超时喵！")
        stores_cache = {}
    except Exception as e:
        logger.error(f"获取商店列表异常: {e}")
        stores_cache = {}
    return stores_cache


async def search_deals(session: aiohttp.ClientSession, title: str, limit: int = 5):
    encoded = urllib.parse.quote(title)
    url = f"https://www.cheapshark.com/api/1.0/deals?title={encoded}&pageSize={limit}"
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with session.get(url, headers=UA, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data if isinstance(data, list) else []
            logger.error(f"搜索折扣失败，状态码: {resp.status}")
            return []
    except asyncio.TimeoutError:
        logger.error("搜索折扣超时喵！")
        return []
    except Exception as e:
        logger.error(f"搜索折扣异常: {e}")
        return []


async def get_hot_deals(session: aiohttp.ClientSession, page_size: int = 60, pages: int = 3):
    all_deals = []
    timeout = aiohttp.ClientTimeout(total=10)
    for page in range(pages):
        url = (
            f"https://www.cheapshark.com/api/1.0/deals"
            f"?sortBy=Discount&sortOrder=desc&pageSize={page_size}&pageNumber={page}"
        )
        try:
            async with session.get(url, headers=UA, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        all_deals.extend(data)
                else:
                    logger.error(f"获取热门折扣失败，状态码: {resp.status} (page {page})")
        except asyncio.TimeoutError:
            logger.error(f"获取热门折扣超时喵！(page {page})")
        except Exception as e:
            logger.error(f"获取热门折扣异常: {e} (page {page})")
    return all_deals


def get_store_name(store_id: str) -> str:
    return stores_cache.get(str(store_id), "Unknown Store")


def is_quality_deal(deal: dict, min_savings: float = 40.0,
                    ratings=('Positive', 'Very Positive'),
                    min_rating_count: int = 100) -> bool:
    try:
        if float(deal.get('savings', '0')) <= min_savings:
            return False
    except (TypeError, ValueError):
        return False

    if ratings is not None and deal.get('steamRatingText', '') not in ratings:
        return False

    if min_rating_count > 0:
        try:
            if int(deal.get('steamRatingCount', '0')) < min_rating_count:
                return False
        except (TypeError, ValueError):
            return False
    return True


def filter_deals_with_fallback(deals: list, min_results: int = 5) -> list:
    tiers = [
        (40.0, ('Positive', 'Very Positive'), 100),
        (30.0, ('Positive', 'Very Positive'), 20),
        (20.0, None, 0),
    ]
    result = []
    for min_savings, ratings, min_count in tiers:
        result = [d for d in deals if is_quality_deal(d, min_savings, ratings, min_count)]
        if len(result) >= min_results:
            return result
    return result


def as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def dedupe_best_per_title(deals: list) -> list:
    groups = {}
    for d in deals:
        title = d.get('title', 'Unknown')
        if title not in groups:
            groups[title] = d
            continue
        cur_savings = as_float(groups[title].get('savings'), 0.0)
        new_savings = as_float(d.get('savings'), 0.0)
        if new_savings > cur_savings:
            groups[title] = d
        elif new_savings == cur_savings:
            if as_float(d.get('salePrice'), 999.0) < as_float(groups[title].get('salePrice'), 999.0):
                groups[title] = d
    best = list(groups.values())
    best.sort(key=lambda x: as_float(x.get('savings'), 0.0), reverse=True)
    return best

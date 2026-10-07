"""定时任务。

discord.ext.tasks 没了，换成 khl.py 自带的 bot.task（底下是 APScheduler）：
  @tasks.loop(time=DEAL_TIME)    -> @bot.task.add_cron(hour=..., minute=..., timezone=CST)
  @tasks.loop(seconds=30)        -> @bot.task.add_interval(seconds=30)
  @tasks.loop(minutes=30)        -> @bot.task.add_interval(minutes=30)

每个任务体都包了 try/except：CheapShark 抽风不该让明天的播报也一起停。
"""
import logging
from datetime import datetime

import aiohttp

from config import (CST, DEAL_HOUR, DEAL_MINUTE, GENERAL_ID, IDLE_TIMEOUT,
                    LOOT_ID, MAX_REPLY_CHARS, MORNING_HOUR, MORNING_MINUTE,
                    WEATHER_CITIES)
from core import ai
from core.db import (complete_reminder, due_reminders, filter_unposted_deals,
                     mark_deals_posted, prune_old_deals)
from core.deals import (dedupe_best_per_title, filter_deals_with_fallback,
                        get_hot_deals, get_stores)
from core.weather import get_lat_lon, get_weather_by_coords
from handlers.common import dm, send_text, voice_channel_humans
from render import cards
from voice import player as vp

logger = logging.getLogger(__name__)

def _broadcast_error(what: str, channel_id, e):
    """播报失败最常见的原因就是频道 id 填错了（比如还留着 Discord 的 id），
    直接甩 traceback 看不出这一点，这里把话说清楚。"""
    msg = str(e)
    logger.error(f"❌ 每日{what}播报失败: {msg}")
    if '不存在' in msg or '权限' in msg:
        logger.error(
            f"   频道 id = {channel_id}。KOOK 的频道 id 是 16 位左右，"
            f"如果这个是 17-19 位，那多半还是 Discord 版的 id。"
            f"跑 `python3 doctor.py` 会直接告诉你哪个填错了。")
    else:
        logger.error("   完整堆栈：", exc_info=e)


def setup(bot, bot_user_id_getter):

    # ---------------- 每日折扣 22:00 ----------------
    @bot.task.add_cron(hour=DEAL_HOUR, minute=DEAL_MINUTE, timezone=CST)
    async def daily_deals_task():
        try:
            if not LOOT_ID:
                return
            async with aiohttp.ClientSession() as session:
                await get_stores(session)
                deals = await get_hot_deals(session, page_size=60, pages=3)

            if not deals:
                await send_text(bot, LOOT_ID, "呜……今天的热门折扣获取失败喵！")
                return

            quality = filter_deals_with_fallback(deals, min_results=5)
            if not quality:
                await send_text(bot, LOOT_ID, "今天没有找到优质折扣喵，明天再来看看吧！")
                return

            best = dedupe_best_per_title(quality)
            try:
                fresh = filter_unposted_deals(best)
            except Exception as e:
                logger.error(f"折扣去重失败，改为全量播报: {e}")
                fresh = best

            if not fresh:
                await send_text(bot, LOOT_ID,
                                "今天没有新的折扣喵，昨天播过的还在打折，用 /hotdeals 看看吧~")
                return

            top = fresh[:25]
            text, posted_count = cards.deal_broadcast_text(top, MAX_REPLY_CHARS)
            await send_text(bot, LOOT_ID, text)

            # 只记真正播出去的那几条。
            # Discord 版这里是 mark_deals_posted(top)，被长度截掉的那些
            # 会被永久标记成已播报，以后再也不会出现。
            try:
                mark_deals_posted(top[:posted_count])
            except Exception as e:
                logger.error(f"记录已播报折扣失败: {e}")

        except Exception as e:
            _broadcast_error('折扣', LOOT_ID, e)

    # ---------------- 每日天气 10:00 ----------------
    @bot.task.add_cron(hour=MORNING_HOUR, minute=MORNING_MINUTE, timezone=CST)
    async def daily_weather_task():
        try:
            if not GENERAL_ID:
                return
            msg = ("(met)all(met) ☀️ **大家早上好喵！新的一天也要元气满满哦~**\n\n"
                   "🌍 **今日关注城市天气播报：**\n")
            async with aiohttp.ClientSession() as session:
                for item in WEATHER_CITIES:
                    lat, lon, zh_name, _ = await get_lat_lon(session, item["city"], item["country"])
                    if not (lat and lon):
                        continue
                    w = await get_weather_by_coords(session, lat, lon)
                    if w and "main" in w:
                        msg += (f"• **{zh_name}**：{w['weather'][0]['description']}，"
                                f"温度 **{w['main']['temp']}°C**\n")
            await send_text(bot, GENERAL_ID, msg)
        except Exception as e:
            _broadcast_error('天气', GENERAL_ID, e)

    # ---------------- 提醒，每 30 秒 ----------------
    @bot.task.add_interval(seconds=30)
    async def reminder_task():
        try:
            rows = due_reminders()
        except Exception as e:
            logger.error(f"读取到期提醒失败: {e}")
            return

        for row in rows:
            text = row['text']
            delivered = False
            try:
                await send_text(bot, row['channel_id'],
                                f"(met){row['user_id']}(met) ⏰ 提醒时间到了喵：**{text}**")
                delivered = True
            except Exception as e:
                logger.error(f"提醒 {row['id']} 发送到频道失败: {e}")

            if not delivered:
                # 频道没了或没权限就退回私信
                delivered = await dm(bot, row['user_id'], f"⏰ 提醒时间到了喵：**{text}**")

            # 不管送没送到都标记完成，否则会无限重试刷屏
            complete_reminder(row['id'])
            if not delivered:
                logger.warning(f"提醒 {row['id']} 无法送达，已标记完成")

    # ---------------- 清理，每 30 分钟 ----------------
    @bot.task.add_interval(minutes=30)
    async def cleanup_task():
        try:
            expired = await ai.cleanup_expired()
            for key in expired:
                logger.warning(f"🧹 清理了会话 {key} 的 AI 上下文（超时）")
        except Exception as e:
            logger.error(f"清理 AI 会话失败: {e}")

        try:
            removed = prune_old_deals()
            if removed:
                logger.info(f"🧹 清理了 {removed} 条过期折扣记录")
        except Exception as e:
            logger.error(f"清理折扣记录失败: {e}")

    # ---------------- 语音闲置检查，每 90 秒 ----------------
    # Discord 版是 60 秒，而且 vc.channel.members 是本地缓存、不花钱。
    # KOOK 这边每个活跃 player 每轮都要打一次 API，所以放慢一点。
    @bot.task.add_interval(seconds=90)
    async def idle_disconnect():
        bot_user_id = bot_user_id_getter()
        now = datetime.now().timestamp()
        for guild_id, player in vp.all_players():
            try:
                if not player.voice_channel_id:
                    if not player.is_playing:
                        vp.drop_player(guild_id)
                    continue

                # 返回 None 表示这次没查出来（接口报错），不当成"没人"，等下一轮再看
                humans = await voice_channel_humans(bot, player.voice_channel_id, bot_user_id)
                if humans == 0:
                    logger.info(f"语音频道没人了，退出 guild={guild_id}")
                    text_channel = player.text_channel_id
                    vp.drop_player(guild_id)
                    if text_channel:
                        await send_text(bot, text_channel, "语音频道没人了，taffy先撤了喵。")
                    continue

                if player.is_idle():
                    logger.info(f"闲置超时，退出语音 guild={guild_id}")
                    text_channel = player.text_channel_id
                    vp.drop_player(guild_id)
                    if text_channel:
                        await send_text(
                            bot, text_channel,
                            "闲置太久了，taffy先退出语音频道喵，想听歌再叫咱。")
            except Exception as e:
                logger.error(f"闲置检查异常 guild={guild_id}: {e}")

"""音乐相关指令。

移植要点
--------
1. 没有 VoiceClient。连语音、推流都在 voice/player.py 里自己做，这里只管指令。
2. 用户在哪个语音频道要打 API 查（Discord 是本地缓存的 user.voice.channel）。
3. /search 的下拉选单变成一排编号按钮，KOOK 卡片没有 select。
4. /pause 和 /resume 做不了，原因见下面的注释。
"""
import asyncio
import logging
import uuid
from datetime import datetime

from khl import Bot, Message

from config import MAX_QUEUE, MAX_REPLY_CHARS, SEARCH_LIMIT, TOKEN
from core.music_source import get_song_data, search_bilibili, search_youtube
from handlers.common import (button_channel_id, button_guild_id,
                             button_user_id, clean_query,
                             find_user_voice_channel, on_button, reply_plain,
                             rest, send_card, send_text, upload_external)
from render import cards
from voice import player as vp

logger = logging.getLogger(__name__)

# search_id -> {results, requester_id, expire_ts, guild_id}
_searches = {}
SEARCH_TIMEOUT = 60

_bot = None


async def _ensure_voice(bot, msg_or_ids):
    """返回 (guild_id, voice_channel_id) 或 (None, None)。"""
    if isinstance(msg_or_ids, tuple):
        guild_id, user_id = msg_or_ids
    else:
        guild_id = getattr(msg_or_ids.ctx.guild, 'id', None) if msg_or_ids.ctx.guild else None
        user_id = msg_or_ids.author_id
    if not guild_id:
        return None, None
    vc = await find_user_voice_channel(bot, guild_id, user_id)
    return str(guild_id), vc


async def _enqueue(bot, guild_id, voice_channel_id, text_channel_id, query):
    """解析并入队。yt_dlp 是同步的，必须丢线程，否则会卡住整个事件循环。"""
    existing = vp.peek_player(guild_id)
    if MAX_QUEUE > 0 and existing is not None and len(existing.queue) >= MAX_QUEUE:
        raise ValueError(f"队列里已经排了 {len(existing.queue)} 首了喵，等放掉几首再点吧~")
    song = await asyncio.to_thread(get_song_data, query)
    player = vp.get_player(guild_id, TOKEN)
    player.on_track_start = _announce
    player.on_error = _announce_error
    player.enqueue(song, voice_channel_id, text_channel_id)
    return song


async def _announce(player, song):
    """新的 player 在开始放一首歌时回调这里。

    以前靠 kookvoice.on_event(Status.START)，那个回调跑在它自己的
    事件循环里；现在播放循环就在主循环里，直接 await 就行。
    """
    if player.text_channel_id and _bot is not None:
        try:
            await send_text(_bot, player.text_channel_id,
                            f"🎵 正在播放: **{song['title']}**")
        except Exception as e:
            logger.error(f"发送正在播放失败: {e}")


async def _announce_error(player, text):
    """歌放不出来 / 进不去语音时告诉频道一声。

    纯文本发：歌名里常有方括号、星号，当 KMarkdown 发会被 KOOK 拒收。
    """
    if player.text_channel_id and _bot is not None:
        await send_text(_bot, player.text_channel_id, text, plain=True)


@on_button('search')
async def _search_button(bot, event, parts):
    if len(parts) < 4 or parts[1] != 'pick':
        return
    search_id, index = parts[2], parts[3]
    channel_id = button_channel_id(event)
    user_id = button_user_id(event)

    entry = _searches.get(search_id)
    if entry is None or entry['expire_ts'] < datetime.now().timestamp():
        _searches.pop(search_id, None)
        await send_text(bot, channel_id, "这个搜索列表已经过期了喵，重新 /search 一下吧~",
                        temp_target_id=user_id)
        return

    if user_id != entry['requester_id']:
        await send_text(bot, channel_id, "这是别人搜出来的列表喵，自己 /search 一个~",
                        temp_target_id=user_id)
        return

    try:
        chosen = entry['results'][int(index)]
    except (IndexError, ValueError):
        await send_text(bot, channel_id, "选项对不上号喵，重新搜一次吧~",
                        temp_target_id=user_id)
        return

    guild_id, vc = await _ensure_voice(bot, (entry['guild_id'], user_id))
    if not vc:
        await send_text(bot, channel_id, "❌ 请先加入一个语音频道。", temp_target_id=user_id)
        return

    _searches.pop(search_id, None)
    try:
        song = await _enqueue(bot, guild_id, vc, channel_id, chosen['url'])
    except Exception as e:
        logger.error(f"搜索结果播放失败: {e}")
        await send_text(bot, channel_id, f"❌ 播放失败了喵：{e}", plain=True)
        return

    await send_text(bot, channel_id, f"✅ 已将 **{song['title']}** 加入队列。")


def setup(bot: Bot):
    global _bot
    _bot = bot

    @bot.command(name='play')
    async def play(msg: Message, *args):
        # KOOK 把粘贴的链接转成了 [文字](链接)，先还原成裸 URL
        query = clean_query(rest(args))
        if not query:
            # /play 只收链接。按歌名找是 /search 的活。
            await msg.reply("要放哪个视频呀喵？用法：`/play 视频链接`（YouTube / B站）。"
                            "想按歌名找请用 `/search 告白气球`", is_temp=True)
            return

        guild_id, vc = await _ensure_voice(bot, msg)
        if not vc:
            await msg.reply("❌ 请先加入一个语音频道。", is_temp=True)
            return

        try:
            song = await _enqueue(bot, guild_id, vc, msg.target_id, query)
        except Exception as e:
            # 纯文本发：报错里带着用户原样的链接，当 KMarkdown 发会被 KOOK 拒收
            await reply_plain(msg, f"❌ {e}")
            return
        await msg.reply(f"✅ 已将 **{song['title']}** 加入队列。")

    @bot.command(name='search')
    async def search(msg: Message, *args):
        """Discord 版用 app_commands.Choice 做平台下拉，KOOK 没有，改成末尾关键词。"""
        args = list(args)
        platform_value = "youtube"
        if args and args[-1].lower() in ('bilibili', 'b站', 'bili'):
            platform_value = "bilibili"
            args = args[:-1]
        query = clean_query(rest(args))
        if not query:
            await msg.reply("要搜什么呀喵？用法：`/search 告白气球` 或 `/search 告白气球 bilibili`",
                            is_temp=True)
            return

        platform_label = "Bilibili" if platform_value == "bilibili" else "YouTube"
        try:
            if platform_value == "bilibili":
                results = await search_bilibili(query, SEARCH_LIMIT)
            else:
                results = await asyncio.to_thread(search_youtube, query, SEARCH_LIMIT)
        except Exception as e:
            logger.error(f"[{platform_label}] 搜索失败 ({query}): {e}")
            await reply_plain(msg, f"❌ 搜索失败了喵：{e}")
            return

        if not results:
            await msg.reply(f"呜……在 {platform_label} 上没搜到 **{query}** 喵！换个词试试？")
            return

        search_id = uuid.uuid4().hex[:12]
        _searches[search_id] = {
            'results': results,
            'requester_id': msg.author_id,
            'guild_id': getattr(msg.ctx.guild, 'id', None) if msg.ctx.guild else None,
            'expire_ts': datetime.now().timestamp() + SEARCH_TIMEOUT,
        }
        await send_card(bot, msg.target_id,
                        cards.search_card(search_id, platform_label, query, results))

    @bot.command(name='skip')
    async def skip(msg: Message, *args):
        guild_id = getattr(msg.ctx.guild, 'id', None) if msg.ctx.guild else None
        player = vp.peek_player(guild_id) if guild_id else None
        if player and player.skip():
            await msg.reply("⏭️ 已跳过。")
        else:
            await msg.reply("❌ 当前没有播放中的歌曲。", is_temp=True)

    @bot.command(name='queue')
    async def show_queue(msg: Message, *args):
        guild_id = getattr(msg.ctx.guild, 'id', None) if msg.ctx.guild else None
        player = vp.peek_player(guild_id) if guild_id else None
        if player is None or (not player.current and not player.queue):
            await msg.reply("📭 队列为空。")
            return
        await msg.reply(cards.queue_card(player.current, player.queue, MAX_REPLY_CHARS - 200))

    @bot.command(name='stop')
    async def stop(msg: Message, *args):
        guild_id = getattr(msg.ctx.guild, 'id', None) if msg.ctx.guild else None
        player = vp.peek_player(guild_id) if guild_id else None
        if player is None:
            await msg.reply("❌ 当前没有在放歌喵。", is_temp=True)
            return
        vp.drop_player(guild_id)
        await msg.reply("🛑 已停止播放，离开了语音频道。")

    @bot.command(name='nowplaying')
    async def now_playing(msg: Message, *args):
        guild_id = getattr(msg.ctx.guild, 'id', None) if msg.ctx.guild else None
        player = vp.peek_player(guild_id) if guild_id else None
        if player is None or not player.current:
            await msg.reply("🔇 当前没有播放音乐。", is_temp=True)
            return
        song = player.current
        # KOOK 卡片不渲染外链图片，封面要先传到 KOOK 的资源服务器
        thumb = await upload_external(bot, song.get('thumbnail'))
        await msg.reply(cards.now_playing_card(song, thumb))

    # ---- /pause 和 /resume ----
    # kookvoice 的播放循环里没有暂停这个概念：ffmpeg 一路往 RTP 推，
    # 只有 skip / stop 两种打断方式。库里的 seek() 理论上能拿来模拟，
    # 但它依赖的 now_playing['ss'] 被写成了负值（上游的 bug），
    # 按它跳转会跳到错误的位置。
    # 与其上线两个半残的指令，不如老实说做不到。

    @bot.command(name='pause')
    async def pause(msg: Message, *args):
        await msg.reply(
            "呜……KOOK 这边的推流不支持暂停喵，只能 `/skip` 或者 `/stop`。"
            "塔菲不想给你一个按了没反应的按钮喵！", is_temp=True)

    @bot.command(name='resume')
    async def resume(msg: Message, *args):
        await msg.reply(
            "KOOK 这边没有暂停，所以也没有恢复喵。想重新听就 `/play` 一次吧~", is_temp=True)

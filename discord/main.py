import os
import sys
import fcntl
import re
import html
import random
import asyncio
import aiohttp
import json
import yt_dlp
import io
import logging
import threading
import urllib.parse
import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
from google import genai
from google.genai import types
from datetime import time, timezone, timedelta, datetime
from time import monotonic

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
GEMINI_KEY = os.getenv('GEMINI_API_KEY')
OWM_KEY = os.getenv('OWM_API_KEY')
DOG_API_KEY = os.getenv('DOG_API_KEY', '')
GENERAL_ID = os.getenv('GENERAL_ID')
LOOT_ID = os.getenv('LOOT_ID')
# 可选：填了就把指令同步到这个服务器，改动立刻生效。
# 不填就走全局同步，Discord 那边最多要等 1 小时才会出现新指令。
GUILD_ID = os.getenv('GUILD_ID')

# ---------- 语言 / Language ----------
# .env 里写 LANGUAGE=zh（默认，简体中文）或 LANGUAGE=en（English）。
# 它决定塔菲发出去的所有固定文案、日志，以及 AI 默认用哪种语言回复。
# 斜杠指令的说明文字另外会跟着每个用户自己的 Discord 语言走（见 CommandTranslator）。
LANG = 'en' if (os.getenv('LANGUAGE') or 'zh').strip().lower().startswith('en') else 'zh'


def L(zh: str, en: str) -> str:
    """按 LANGUAGE 选一句。中英文写在一起，改文案时不容易漏掉另一边。"""
    return en if LANG == 'en' else zh


_CMD_TEXT = {}


def D(zh: str, en: str) -> str:
    """斜杠指令 / 参数的说明文字。

    和 L 一样按 LANGUAGE 返回默认文案，同时把中英两版登记下来，
    CommandTranslator 同步指令时会把它们分别交给 Discord：
    客户端是中文的用户看到中文，是英文的看到英文，其他语言看到默认那一版。
    """
    _CMD_TEXT[zh] = _CMD_TEXT[en] = (zh, en)
    return L(zh, en)


class CommandTranslator(app_commands.Translator):
    async def translate(self, string, locale, context):
        pair = _CMD_TEXT.get(string.message)
        if pair is None:
            return None   # 指令名、参数名这些不翻译
        if locale in (discord.Locale.chinese, discord.Locale.taiwan_chinese):
            return pair[0]
        if locale in (discord.Locale.american_english, discord.Locale.british_english):
            return pair[1]
        return None


UNKNOWN = L('未知', 'Unknown')

_lock_fp = open('/tmp/taffybot.lock', 'w')
try:
    fcntl.flock(_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print(L("⚠️ 检测到已有一个塔菲实例在运行，本进程退出喵！", "⚠️ Another Taffy instance is already running; exiting, nya!"))
    sys.exit(1)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="/", intents=intents)

user_chats = {}
USER_CHAT_TIMEOUT = 3600
user_last_active = {}
user_chat_lock = asyncio.Lock()
cookie_path = os.path.abspath('cookies.txt')

ffmpeg_log_path = os.path.abspath('ffmpeg_debug.log')
ffmpeg_log = open(ffmpeg_log_path, 'a', encoding='utf-8')
FFMPEG_LOG_MAX_BYTES = 10 * 1024 * 1024



def _env_num(name: str, default, cast=int):
    """读数字型环境变量，没填或填错就用默认值。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return cast(raw.strip())
    except ValueError:
        print(L(f"⚠️ .env 里的 {name}={raw!r} 不是数字，先用默认值 {default}", f"⚠️ {name}={raw!r} in .env is not a number; using the default {default}"))
        return default


# ---------- 用量上限 ----------
# 默认值都给得很宽，朋友间的小服务器基本碰不到；
# 挂到人多的服务器上时，它们能挡住刷屏、爆内存和 AI 账单失控。
# 全部可以在 .env 里改，填 0 就是关掉这一项限制。
MAX_QUEUE = _env_num('MAX_QUEUE', 50)                      # 每个服务器最多排多少首歌
MAX_IMAGE_MB = _env_num('MAX_IMAGE_MB', 8.0, float)        # 发给 AI 的单张图片大小上限（MB）
MAX_IMAGES_PER_MESSAGE = _env_num('MAX_IMAGES_PER_MESSAGE', 4)   # 一条消息最多带几张图给 AI
AI_COOLDOWN_SECONDS = _env_num('AI_COOLDOWN_SECONDS', 3.0, float)  # 同一个人两次 AI 回复的最短间隔
AI_DAILY_LIMIT = _env_num('AI_DAILY_LIMIT', 0)             # 每天最多调多少次 AI，0 = 不限

# ---------- 每日天气播报的城市 ----------
# 默认是三个通用的大城市。想换成自己关心的，在 .env 里写：
#   WEATHER_CITIES=Shanghai,CN;New York,US;Tokyo,JP
# 城市之间用分号隔开，每个写成「城市名,国家代码」，国家代码可以不写。
DEFAULT_WEATHER_CITIES = "Shanghai,CN;New York,US;Tokyo,JP"


def parse_weather_cities(raw: str) -> list:
    cities = []
    for part in (raw or '').replace('；', ';').replace('，', ',').split(';'):
        city, _, country = part.strip().partition(',')
        if city.strip():
            cities.append({"city": city.strip(), "country": country.strip().upper()})
    return cities


WEATHER_CITIES = (parse_weather_cities(os.getenv('WEATHER_CITIES'))
                  or parse_weather_cities(DEFAULT_WEATHER_CITIES))

# /play 只接受这些站的链接（子域名也算）。
# 不设白名单的话 yt-dlp 什么地址都会去抓，包括机器人所在机器的内网地址。
PLAY_ALLOWED_HOSTS = ('youtube.com', 'youtu.be', 'bilibili.com', 'b23.tv')

# yt-dlp 每次解析完都会把 cookies.txt 整个重写一遍。
# 两个解析同时跑、或者一边写一边读，读到的就是半个文件。
# 所以所有碰 cookies.txt 的地方都排这一把锁；用 RLock 是因为解析过程中还会回头读 cookie。
_ytdl_lock = threading.RLock()

ai_last_reply = {}                       # user_id -> 上次 AI 回复的时间（monotonic）
ai_daily = {'day': None, 'count': 0}


def ai_rate_check(user_id) -> str:
    """返回 'ok' / 'cooldown' / 'daily'。返回 'ok' 时这一次已经记上账了。"""
    now = monotonic()
    last = ai_last_reply.get(user_id)
    if AI_COOLDOWN_SECONDS > 0 and last is not None and now - last < AI_COOLDOWN_SECONDS:
        return 'cooldown'
    ai_last_reply[user_id] = now

    today = datetime.now().date()
    if ai_daily['day'] != today:
        ai_daily['day'], ai_daily['count'] = today, 0
    if AI_DAILY_LIMIT > 0 and ai_daily['count'] >= AI_DAILY_LIMIT:
        return 'daily'
    ai_daily['count'] += 1
    return 'ok'


def check_play_url(query: str):
    """不是白名单里的链接就抛 ValueError。歌名、内网地址、别的网站都会被挡在这。"""
    text = (query or '').strip()
    host, scheme = '', ''
    # 反斜杠、空白、控制字符：不同的 URL 解析器对它们理解不一样，直接不收
    if text and not any(c in text for c in '\\ \t\r\n') and text.isprintable():
        try:
            parts = urllib.parse.urlsplit(text)
            host, scheme = (parts.hostname or '').lower(), parts.scheme.lower()
        except ValueError:
            pass
    if scheme in ('http', 'https') and any(
            host == d or host.endswith('.' + d) for d in PLAY_ALLOWED_HOSTS):
        return
    raise ValueError(L("/play 只收 YouTube / B站 的链接喵。想按歌名找请用 /search", "/play only takes YouTube / Bilibili links, nya. To find a song by name, use /search"))


# 语音闲置多久后自动退出语音频道（秒）
IDLE_TIMEOUT = 300
# 歌曲直链超过这个秒数就在播放前重新解析，避免 CDN 链接过期
URL_REFRESH_AFTER = 1800


def rotate_ffmpeg_log():
    """FFmpeg 的 stderr 会一直追加，超过上限就轮转一次，避免无限增长。"""
    global ffmpeg_log
    try:
        ffmpeg_log.flush()
        if os.path.getsize(ffmpeg_log_path) > FFMPEG_LOG_MAX_BYTES:
            ffmpeg_log.close()
            os.replace(ffmpeg_log_path, ffmpeg_log_path + '.1')
            ffmpeg_log = open(ffmpeg_log_path, 'a', encoding='utf-8')
    except Exception as e:
        logger.error(L(f"轮转 ffmpeg 日志失败: {e}", f"Failed to rotate the ffmpeg log: {e}"))


@tasks.loop(minutes=30)
async def cleanup_expired_chats():
    now = datetime.now().timestamp()
    async with user_chat_lock:
        expired_keys = [
            key for key, last_ts in user_last_active.items()
            if now - last_ts > USER_CHAT_TIMEOUT
        ]
        for key in expired_keys:
            user_chats.pop(key, None)
            user_last_active.pop(key, None)
    # 冷却记录用完就没用了，顺手清掉，不然每个说过话的人都会留一条
    stale_before = monotonic() - max(AI_COOLDOWN_SECONDS, 60)
    for uid in [u for u, ts in ai_last_reply.items() if ts < stale_before]:
        ai_last_reply.pop(uid, None)
    for key in expired_keys:
        logger.warning(L(f"🧹 清理了会话 {key} 的 AI 上下文（超时）", f"🧹 Cleared the AI context for session {key} (timed out)"))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

logging.getLogger('discord').setLevel(logging.WARNING)

@bot.event
async def on_ready():
    logger.info(L(f"系统已上线。当前登录身份: {bot.user.name}", f"Online. Logged in as: {bot.user.name}"))
    logger.info('------')

    if not daily_deals_task.is_running():
        daily_deals_task.start()

    if not daily_weather_task.is_running():
        daily_weather_task.start()

    if not bot.get_cog('MusicCog'):
        await bot.add_cog(MusicCog(bot))
        logger.info(L("音乐 Cog 已加载喵！", "Music cog loaded, nya!"))

    try:
        # 要在 sync 之前装上，sync 时才会把中英两版说明一起交给 Discord
        if bot.tree.translator is None:
            await bot.tree.set_translator(CommandTranslator())
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            # copy_global_to 把全局指令复制一份到这个服务器，同步后立刻可见
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            logger.info(L(f"成功同步了 {len(synced)} 个斜杠指令到服务器 {GUILD_ID} 喵！（立刻生效）", f"Synced {len(synced)} slash commands to server {GUILD_ID}, nya! (effective immediately)"))
        else:
            synced = await bot.tree.sync()
            logger.info(L(
                f"成功同步了 {len(synced)} 个全局斜杠指令喵！"
                "（全局同步最多要等 1 小时才会出现，想立刻生效就在 .env 里配 GUILD_ID）",
                f"Synced {len(synced)} global slash commands, nya! "
                "(global sync can take up to 1 hour to show up; set GUILD_ID in .env for instant updates)"
            ))
        logger.info(L("已同步指令: ", "Synced commands: ") + ", ".join(sorted(c.name for c in synced)))
    except Exception as e:
        logger.error(L(f"同步斜杠指令失败喵: {e}", f"Failed to sync slash commands, nya: {e}"))

    if GEMINI_KEY and client:
        bot.aio_client = client.aio
        logger.info(L("异步 Gemini 客户端已就绪喵！", "Async Gemini client ready, nya!"))

    logger.info(L(f"正在使用 Cookie 文件: {cookie_path}", f"Using cookie file: {cookie_path}"))
    if not os.path.isfile(cookie_path):
        logger.error(L(f"⚠️ 警告：Cookie 文件不存在 ({cookie_path})，音乐播放功能将不可用！", f"⚠️ Warning: cookie file not found ({cookie_path}); music playback will not work!"))

    logger.warning(L("正在清理聊天记录喵。", "Starting chat-history cleanup, nya."))
    if not cleanup_expired_chats.is_running():
        cleanup_expired_chats.start()

    ai_on = bool(GEMINI_KEY and client)
    cookie_ok = os.path.isfile(cookie_path)
    weather_on, deals_on = daily_weather_task.is_running(), daily_deals_task.is_running()
    cleanup_on = cleanup_expired_chats.is_running()
    status_report = [
        L("=== 塔菲 Bot 启动健康报告 ===", "=== Taffy Bot startup health report ==="),
        L(f"🤖 机器人：{bot.user.name} (ID: {bot.user.id})",
          f"🤖 Bot: {bot.user.name} (ID: {bot.user.id})"),
        L(f"📡 延迟：{round(bot.latency * 1000)}ms", f"📡 Latency: {round(bot.latency * 1000)}ms"),
        f"🌐 语言 / Language: {LANG}",
        L(f"🧠 AI 功能：{'✅ 已启用' if ai_on else '❌ 未配置'}",
          f"🧠 AI chat: {'✅ enabled' if ai_on else '❌ not configured'}"),
        L(f"🎵 音乐播放：{'✅ Cookie 有效' if cookie_ok else '❌ Cookie 缺失'}",
          f"🎵 Music: {'✅ cookie file found' if cookie_ok else '❌ cookie file missing'}"),
        L(f"☁️  天气功能：{'✅ 已配置' if OWM_KEY else '❌ 未配置'}",
          f"☁️  Weather: {'✅ configured' if OWM_KEY else '❌ not configured'}"),
        L(f"💰 折扣查询：{'✅ 已启用' if LOOT_ID else '❌ 未配置播报频道'}",
          f"💰 Deals: {'✅ enabled' if LOOT_ID else '❌ no broadcast channel set'}"),
        L(f"🔄 指令同步：{('✅ 服务器 ' + str(GUILD_ID) + '（立刻生效）') if GUILD_ID else '🌐 全局同步（新指令最多等 1 小时）'}",
          f"🔄 Command sync: {('✅ server ' + str(GUILD_ID) + ' (instant)') if GUILD_ID else '🌐 global (new commands can take up to 1 hour)'}"),
        L(f"📊 定时任务：{'✅ 每日天气' if weather_on else '❌ 天气任务未启动'} | {'✅ 每日折扣' if deals_on else '❌ 折扣任务未启动'}",
          f"📊 Scheduled: {'✅ daily weather' if weather_on else '❌ weather task not running'} | {'✅ daily deals' if deals_on else '❌ deals task not running'}"),
        L(f"🧹 清理任务：{'✅ 运行中' if cleanup_on else '❌ 未启动'}",
          f"🧹 Cleanup: {'✅ running' if cleanup_on else '❌ not running'}"),
        "================================"
    ]
    for line in status_report:
        logger.info(line)

class MusicPlayer:
    def __init__(self, guild_id=None):
        self.guild_id = guild_id
        self.text_channel = None
        self.voice_client = None
        self.queue = asyncio.Queue()
        self.current_song = None
        self.is_playing = False
        self.stopped = False
        self.lock = asyncio.Lock()
        self.idle_since = datetime.now().timestamp()

    async def _mark_idle(self):
        self.is_playing = False
        self.current_song = None
        self.idle_since = datetime.now().timestamp()

    async def _refresh_if_stale(self, song):
        """直链有有效期，排队太久就在播放前重新解析一次。"""
        resolved_at = song.get('resolved_at', 0)
        if datetime.now().timestamp() - resolved_at < URL_REFRESH_AFTER:
            return song
        page_url = song.get('webpage_url')
        if not page_url:
            return song
        try:
            fresh = await asyncio.to_thread(get_song_data, page_url)
            logger.info(L(f"直链已过期，重新解析成功: {fresh.get('title')}", f"Stream link expired; re-resolved: {fresh.get('title')}"))
            return fresh
        except Exception as e:
            logger.error(L(f"重新解析直链失败，沿用旧链接: {e}", f"Failed to re-resolve the stream link; using the old one: {e}"))
            return song

    def _after_playback(self, error):
        """FFmpeg 播放结束回调（在 FFmpeg 线程里跑，不能直接 await）。"""
        if error:
            logger.error(L(f"FFmpeg 播放异常: {error}", f"FFmpeg playback error: {error}"))
        try:
            asyncio.run_coroutine_threadsafe(self.play_next(), bot.loop)
        except Exception as e:
            logger.error(L(f"调度下一首失败: {e}", f"Failed to schedule the next song: {e}"))

    async def play_next(self):
        async with self.lock:
            if self.stopped:
                return
            if self.queue.empty():
                await self._mark_idle()
                return
            song = await self.queue.get()
            self.current_song = song
            self.is_playing = True

        song = await self._refresh_if_stale(song)

        async with self.lock:
            # stop() 可能在解析期间发生，重新确认一次再碰 voice_client
            if self.stopped:
                return
            vc = self.voice_client
            if vc is None or not vc.is_connected():
                logger.warning(L("语音连接已断开，停止播放喵。", "Voice connection lost; stopping playback, nya."))
                await self._mark_idle()
                return
            self.current_song = song

        http_headers = song.get('http_headers') or {'User-Agent': BROWSER_UA}

        headers_str = ''.join([f"{k}: {v}\r\n" for k, v in http_headers.items()])
        # discord.py 用 shlex 切 before_options，值里带双引号/反斜杠（有些 cookie 就带）
        # 不转义的话引号会被吃掉，带空格时整条 -headers 还会被切碎。
        headers_str = headers_str.replace('\\', '\\\\').replace('"', '\\"')

        before_opts = (
            "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
            "-reconnect_on_network_error 1 -reconnect_on_http_error 4xx,5xx "
            f'-headers "{headers_str}"'
        )

        rotate_ffmpeg_log()

        try:
            source = discord.FFmpegPCMAudio(
                song['url'],
                before_options=before_opts,
                options="-vn",
                stderr=ffmpeg_log
            )
            vc.play(source, after=self._after_playback)
        except Exception as e:
            logger.error(L(f"启动播放失败: {e}", f"Failed to start playback: {e}"))
            async with self.lock:
                await self._mark_idle()
            if self.text_channel:
                try:
                    await self.text_channel.send(L(f"❌ 播放 **{song.get('title', UNKNOWN)}** 失败了喵！", f"❌ Failed to play **{song.get('title', UNKNOWN)}**, nya!"))
                except Exception as send_err:
                    logger.error(L(f"发送播放失败提示失败: {send_err}", f"Failed to send the playback-failure notice: {send_err}"))
            # 这首起不来就接着放下一首，不然后面排着的歌会一直卡到有人再 /play。
            # 只有语音还连着、而且确实没在放东西时才继续，
            # 否则（比如 "Already playing audio"）会把整个队列一首首报错清空。
            if (not self.stopped and vc.is_connected()
                    and not vc.is_playing() and not vc.is_paused()):
                await self.play_next()
            return

        if self.text_channel:
            await self.text_channel.send(L(f"🎵 正在播放: **{song['title']}**", f"🎵 Now playing: **{song['title']}**"))

    async def add_song(self, song_data):
        self.stopped = False
        await self.queue.put(song_data)
        if not self.is_playing:
            await self.play_next()

    async def skip(self):
        if self.voice_client and self.is_playing:
            self.voice_client.stop()
            return True
        return False

    async def pause(self):
        """返回 'ok' / 'already'（本来就暂停着）/ 'none'（没在放歌）。"""
        vc = self.voice_client
        if not vc or not self.is_playing:
            return 'none'
        if vc.is_paused():
            return 'already'
        vc.pause()
        return 'ok'

    async def resume(self):
        """返回 'ok' / 'not_paused'（没暂停，正常放着）/ 'none'（没在放歌）。"""
        vc = self.voice_client
        if not vc or not self.is_playing:
            return 'none'
        if not vc.is_paused():
            return 'not_paused'
        vc.resume()
        return 'ok'

    async def stop(self):
        self.stopped = True
        vc = self.voice_client
        self.voice_client = None
        if vc:
            try:
                vc.stop()
            except Exception as e:
                logger.error(L(f"停止播放失败: {e}", f"Failed to stop playback: {e}"))
            try:
                if vc.is_connected():
                    await vc.disconnect(force=True)
            except Exception as e:
                logger.error(L(f"断开语音连接失败: {e}", f"Failed to disconnect from voice: {e}"))
        self.queue = asyncio.Queue()
        self.is_playing = False
        self.current_song = None
        self.text_channel = None
        self.idle_since = datetime.now().timestamp()


def get_song_data(query: str) -> dict:
    """同步函数，调用方必须用 asyncio.to_thread 包起来。"""
    check_play_url(query)
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'user_agent': BROWSER_UA,
        'sleep_interval': 5,
        'max_sleep_interval': 10,
        'geo_bypass': True,
        'source_address': '0.0.0.0',
        # key 必须是 http_headers，写成 'headers' yt-dlp 不认
        'http_headers': {'User-Agent': BROWSER_UA},
        'age_limit': 0,
    }
    if os.path.isfile(cookie_path):
        ydl_opts['cookiefile'] = cookie_path

    # 先拿锁再开 YoutubeDL：它退出时会重写 cookies.txt，要在锁里完成
    with _ytdl_lock, yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(query, download=False)
            if 'entries' in info:
                info = info['entries'][0]

            # 拿直链。**绝对不能**回落到 webpage_url：
            # 那是播放页的 HTML，喂给 ffmpeg 只会得到 "Invalid data found"。
            direct_url = info.get('url')
            if not direct_url:
                direct_url = pick_audio_format(info)
            if not direct_url:
                page = info.get('webpage_url') or query
                raise ValueError(L(
                    "拿不到音频直链喵。常见原因：视频需要登录/年龄限制/地区封锁，"
                    "或者 yt-dlp 版本太旧跟不上网站改版。"
                    "先试 `pip install -U yt-dlp`，还不行就换一个视频。"
                    f"（{page}）",
                    "Couldn't get a direct audio link, nya. Usual causes: the video needs a login, "
                    "is age- or region-restricted, or yt-dlp is too old for a site change. "
                    "Try `pip install -U yt-dlp` first; if that doesn't help, pick another video. "
                    f"({page})"
                ))

            extractor = (info.get('extractor') or '').lower()
            if 'youtube' in extractor or 'googlevideo.com' in direct_url:
                source = 'youtube'
            elif 'bilibili' in extractor or 'bilivideo' in direct_url:
                source = 'bilibili'
            else:
                source = 'unknown'

            http_headers = build_stream_headers(source, info.get('http_headers'))

            logger.info(L(
                f"[{source}] 音频URL获取成功 -> Referer={http_headers.get('Referer')} | "
                f"含Cookie={'是' if http_headers.get('Cookie') else '否'}",
                f"[{source}] audio URL resolved -> Referer={http_headers.get('Referer')} | "
                f"cookie sent={'yes' if http_headers.get('Cookie') else 'no'}"
            ))

            return {
                'title': info.get('title', L('未知歌曲', 'Unknown song')),
                'url': direct_url,
                'webpage_url': info.get('webpage_url'),
                'resolved_at': datetime.now().timestamp(),
                'duration': info.get('duration', 0),
                'uploader': info.get('uploader', UNKNOWN),
                'thumbnail': info.get('thumbnail'),
                'source': source,
                'http_headers': http_headers,
            }
        except Exception as e:
            raise ValueError(L(f"获取音频失败: {e}", f"Couldn't get the audio: {e}"))

BROWSER_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
)
SEARCH_LIMIT = 5
BILI_SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"


def get_cookies_for_domain(domain_suffix: str) -> str:
    """只取某个域名下的 cookie。

    搜索 API 和取流（build_stream_headers）都走这里：
    每个站只拿到它自己的 cookie，别把两边的登录态混着发出去。
    """
    if not os.path.isfile(cookie_path):
        return ''
    pairs = []
    try:
        # 和 yt-dlp 重写 cookies.txt 互斥。会等锁，所以协程里要用 asyncio.to_thread 调
        with _ytdl_lock, open(cookie_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or not line:
                    continue
                parts = line.split('\t')
                if len(parts) < 7:
                    continue
                domain = parts[0].lstrip('.').lower()
                if domain == domain_suffix or domain.endswith('.' + domain_suffix):
                    pairs.append(f"{parts[5]}={parts[6]}")
    except Exception as e:
        logger.error(L(f"读取 {domain_suffix} cookie 失败: {e}", f"Failed to read {domain_suffix} cookies: {e}"))
        return ''
    return '; '.join(pairs)


# 取流时用的基础请求头。注意这里**不要**写死 Referer，
# 一个站的 Referer 和登录态不能发给另一个站的 CDN。
BASE_HEADERS = {
    'User-Agent': BROWSER_UA,
    'Accept': '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Connection': 'keep-alive',
}

# 每个站自己的 Referer 和 cookie 域，按来源分开取，不要混着发
SOURCE_RULES = {
    'bilibili': {'referer': 'https://www.bilibili.com/', 'cookie_domains': ['bilibili.com']},
    'youtube': {'referer': None, 'cookie_domains': ['youtube.com', 'google.com']},
}


def build_stream_headers(source: str, ytdl_headers: dict = None) -> dict:
    """按来源拼取流请求头：只带这个站自己的 Referer 和 cookie。

    优先用 yt-dlp 给的 http_headers，缺的才用 BASE_HEADERS 补。
    认不出来源的站（unknown）一律不带 cookie。
    """
    headers = dict(ytdl_headers or {})
    for k, v in BASE_HEADERS.items():
        headers.setdefault(k, v)

    rule = SOURCE_RULES.get(source, {})
    referer = rule.get('referer')
    if referer:
        headers.setdefault('Referer', referer)
    elif 'bilibili.com' in (headers.get('Referer') or ''):
        # 非 B 站就把 B 站的 Referer 清掉，别让它跟着跑到别家 CDN
        headers.pop('Referer', None)

    cookie = '; '.join(
        c for c in (get_cookies_for_domain(d) for d in rule.get('cookie_domains', [])) if c
    )
    if cookie:
        headers['Cookie'] = cookie
    else:
        headers.pop('Cookie', None)
    return headers


def pick_audio_format(info: dict):
    """yt-dlp 没直接给 url 时，自己从 formats 里挑一个音频流的直链，挑不出来返回 None。"""
    usable = [f for f in (info.get('formats') or [])
              if f.get('url') and f.get('acodec') not in (None, 'none')]
    if not usable:
        return None
    # 优先纯音频流，没有的话退而求其次用带视频的（ffmpeg 会只取音轨）
    audio_only = [f for f in usable if f.get('vcodec') in ('none', None)]
    best = max(audio_only or usable, key=lambda f: f.get('abr') or f.get('tbr') or 0)
    return best.get('url')


def format_duration(value) -> str:
    """B站给的是 "mm:ss" 字符串，YouTube 给的是秒数，统一成 mm:ss。"""
    if value is None or value == '':
        return UNKNOWN
    if isinstance(value, str):
        value = value.strip()
        # B站给的是 "12:07" / "1:02:05" 这种，其他字符串一律当未知
        if re.fullmatch(r'\d{1,2}(:\d{1,2}){1,2}', value):
            return value
        return UNKNOWN
    try:
        total = int(float(value))
    except (TypeError, ValueError):
        return UNKNOWN
    if total <= 0:
        return UNKNOWN
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def search_youtube(query: str, limit: int = SEARCH_LIMIT) -> list:
    """同步函数，调用方必须用 asyncio.to_thread 包起来。

    extract_flat 只拿搜索页的元数据，不解析每个视频的直链，所以很快；
    这里也不带 sleep_interval，不然搜一次要等十几秒。
    """
    opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': 'in_playlist',
        'skip_download': True,
        'user_agent': BROWSER_UA,
        'geo_bypass': True,
        'source_address': '0.0.0.0',
    }
    if os.path.isfile(cookie_path):
        opts['cookiefile'] = cookie_path

    with _ytdl_lock, yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)

    results = []
    for entry in (info or {}).get('entries') or []:
        if not entry:
            continue
        url = entry.get('url') or entry.get('webpage_url')
        video_id = entry.get('id')
        if not url and video_id:
            url = f"https://www.youtube.com/watch?v={video_id}"
        if not url:
            continue
        results.append({
            'title': entry.get('title') or L('未知视频', 'Unknown video'),
            'url': url,
            'uploader': entry.get('uploader') or entry.get('channel') or UNKNOWN,
            'duration': entry.get('duration'),
            'source': 'youtube',
        })
    return results[:limit]


async def search_bilibili(query: str, limit: int = SEARCH_LIMIT) -> list:
    """走 B 站自己的搜索 API。

    yt-dlp 的 bilisearch 只 yield url_result(arcurl, 'BiliBili', aid)，
    扁平抽取拿不到标题，选单里只会显示一串 av 号，所以这里直接打 API。
    """
    cookie = await asyncio.to_thread(get_cookies_for_domain, 'bilibili.com')
    headers = {
        'User-Agent': BROWSER_UA,
        'Referer': 'https://search.bilibili.com/',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    }
    if cookie:
        headers['Cookie'] = cookie

    params = {
        'search_type': 'video',
        'keyword': query,
        'page': 1,
    }
    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession() as session:
        async with session.get(BILI_SEARCH_URL, params=params,
                               headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                raise ValueError(L(f"B站搜索请求失败，状态码 {resp.status}", f"Bilibili search request failed with status {resp.status}"))
            data = await resp.json(content_type=None)

    code = data.get('code')
    if code != 0:
        # -412 是风控，通常意味着 cookies.txt 里的 B 站 cookie 过期了
        if code == -412:
            raise ValueError(L("B站搜索被风控了喵，可能要更新 cookies.txt 里的 B 站 cookie。", "Bilibili search was blocked by anti-bot checks, nya; the Bilibili cookies in cookies.txt may need refreshing."))
        raise ValueError(L(f"B站搜索返回异常 (code={code}): {data.get('message', '未知错误')}", f"Bilibili search returned an error (code={code}): {data.get('message', 'unknown error')}"))

    raw_results = ((data.get('data') or {}).get('result')) or []

    results = []
    for item in raw_results:
        if item.get('type') and item.get('type') != 'video':
            continue
        bvid = item.get('bvid')
        url = f"https://www.bilibili.com/video/{bvid}" if bvid else item.get('arcurl')
        if not url:
            continue
        # 搜索结果的 title 里带 <em class="keyword"> 高亮标签，要清掉
        title = html.unescape(re.sub(r'<[^>]+>', '', item.get('title') or '')).strip()
        results.append({
            'title': title or L('未知视频', 'Unknown video'),
            'url': url,
            'uploader': item.get('author') or UNKNOWN,
            'duration': item.get('duration'),
            'source': 'bilibili',
        })
        if len(results) >= limit:
            break
    return results


class SearchResultView(discord.ui.View):
    """搜索结果下拉选单，选中后直接进播放队列。"""

    def __init__(self, cog, requester_id: int, results: list, timeout_seconds: int = 60):
        super().__init__(timeout=timeout_seconds)
        self.cog = cog
        self.requester_id = requester_id
        self.results = results
        self.message = None

        options = []
        for index, item in enumerate(results):
            label = (item.get('title') or L(f"结果 {index + 1}", f"Result {index + 1}"))[:95]
            description = f"{item.get('uploader', UNKNOWN)} · {format_duration(item.get('duration'))}"[:95]
            options.append(discord.SelectOption(
                label=label,
                value=str(index),
                description=description,
            ))

        self.select = discord.ui.Select(
            placeholder=L("选一首让 taffy 放喵～", "Pick one for Taffy to play, nya~"),
            options=options,
            custom_id="search_pick",
        )
        self.select.callback = self.on_pick
        self.add_item(self.select)

    async def _disable(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    async def on_pick(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                L("这是别人搜出来的列表喵，自己 /search 一个~",
                  "That's someone else's search list, nya — run your own /search~"),
                ephemeral=True
            )
            return

        try:
            index = int(self.select.values[0])
            chosen = self.results[index]
        except (IndexError, ValueError, TypeError):
            await interaction.response.send_message(L("选项对不上号喵，重新搜一次吧~", "That option doesn't match, nya — try searching again~"), ephemeral=True)
            return

        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(L("❌ 请先加入一个语音频道。", "❌ Join a voice channel first."), ephemeral=True)
            return

        await interaction.response.defer()
        await self._disable()
        self.stop()

        try:
            voice_client = await self.cog.connect_to_user_voice(interaction)
            if voice_client is None:
                await interaction.followup.send(L("❌ 请先加入一个语音频道。", "❌ Join a voice channel first."), ephemeral=True)
                return
            song_data = await self.cog.enqueue_song(interaction, chosen['url'], voice_client)
        except Exception as e:
            logger.error(L(f"搜索结果播放失败: {e}", f"Failed to play the search result: {e}"))
            await interaction.followup.send(L(f"❌ 播放失败了喵：{e}", f"❌ Couldn't play that, nya: {e}"))
            return

        await interaction.followup.send(L(f"✅ 已将 **{song_data['title']}** 加入队列。", f"✅ Added **{song_data['title']}** to the queue."))

    async def on_timeout(self):
        await self._disable()


class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    async def cog_load(self):
        if not self.idle_disconnect.is_running():
            self.idle_disconnect.start()

    async def cog_unload(self):
        self.idle_disconnect.cancel()

    def get_player(self, guild_id):
        if guild_id not in self.players:
            self.players[guild_id] = MusicPlayer(guild_id)
        return self.players[guild_id]

    async def teardown_player(self, guild_id, player, notice=None):
        """断开语音、清空队列，并把 player 从表里摘掉。"""
        text_channel = player.text_channel
        await player.stop()
        self.players.pop(guild_id, None)
        if notice and text_channel:
            try:
                await text_channel.send(notice)
            except Exception as e:
                logger.error(L(f"发送退出提示失败: {e}", f"Failed to send the leave notice: {e}"))

    @tasks.loop(seconds=60)
    async def idle_disconnect(self):
        """没人听或闲置太久就退出语音频道，避免塔菲一直挂在语音里。"""
        now = datetime.now().timestamp()
        for guild_id, player in list(self.players.items()):
            try:
                vc = player.voice_client
                if vc is None or not vc.is_connected():
                    if not player.is_playing:
                        self.players.pop(guild_id, None)
                    continue

                humans = [m for m in vc.channel.members if not m.bot]
                if not humans:
                    logger.info(L(f"语音频道没人了，退出 guild={guild_id}", f"Voice channel is empty; leaving guild={guild_id}"))
                    await self.teardown_player(
                        guild_id, player, L("语音频道没人了，taffy先撤了喵。", "Nobody's left in voice, so Taffy's heading out, nya.")
                    )
                    continue

                idle = (
                    not player.is_playing
                    and player.queue.empty()
                    and player.idle_since is not None
                    and now - player.idle_since > IDLE_TIMEOUT
                )
                if idle:
                    logger.info(L(f"闲置超时，退出语音 guild={guild_id}", f"Idle timeout; leaving voice guild={guild_id}"))
                    await self.teardown_player(
                        guild_id, player, L("闲置太久了，taffy先退出语音频道喵，想听歌再叫咱。", "Been idle too long, so Taffy's leaving voice, nya. Call me when you want music.")
                    )
            except Exception as e:
                logger.error(L(f"闲置检查异常 guild={guild_id}: {e}", f"Idle check error guild={guild_id}: {e}"))

    @idle_disconnect.before_loop
    async def before_idle_disconnect(self):
        await self.bot.wait_until_ready()

    async def connect_to_user_voice(self, interaction: discord.Interaction):
        """连到发起者所在的语音频道，没在语音里就返回 None。"""
        voice_state = interaction.user.voice
        if not voice_state or not voice_state.channel:
            return None
        channel = voice_state.channel
        voice_client = interaction.guild.voice_client
        if voice_client is None:
            voice_client = await channel.connect()
        elif voice_client.channel != channel:
            await voice_client.move_to(channel)
        return voice_client

    async def enqueue_song(self, interaction: discord.Interaction, query: str, voice_client):
        """解析并入队。yt_dlp 是同步的，必须丢线程，否则会卡住整个事件循环。"""
        player = self.get_player(interaction.guild_id)
        if MAX_QUEUE > 0 and player.queue.qsize() >= MAX_QUEUE:
            raise ValueError(L(f"队列里已经排了 {player.queue.qsize()} 首了喵，等放掉几首再点吧~", f"There are already {player.queue.qsize()} songs queued, nya — let a few play before adding more~"))
        song_data = await asyncio.to_thread(get_song_data, query)
        player.voice_client = voice_client
        player.text_channel = interaction.channel
        await player.add_song(song_data)
        return song_data

    @app_commands.command(name="play", description=D("播放音乐（只收 YouTube / B站链接，按歌名找请用 /search）", "Play music (YouTube / Bilibili links only; use /search to find by name)"))
    @app_commands.describe(query=D("视频链接（URL）", "Video link (URL)"))
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str):
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(L("❌ 请先加入一个语音频道。", "❌ Join a voice channel first."), ephemeral=True)
            return

        await interaction.response.defer()

        voice_client = await self.connect_to_user_voice(interaction)
        if voice_client is None:
            await interaction.followup.send(L("❌ 请先加入一个语音频道。", "❌ Join a voice channel first."), ephemeral=True)
            return

        try:
            song_data = await self.enqueue_song(interaction, query, voice_client)
        except Exception as e:
            await interaction.followup.send(f"❌ {e}")
            return

        await interaction.followup.send(L(f"✅ 已将 **{song_data['title']}** 加入队列。", f"✅ Added **{song_data['title']}** to the queue."))

    @app_commands.command(name="search", description=D("搜索歌曲，挑一首让塔菲放喵！", "Search for a song and pick one for Taffy to play, nya!"))
    @app_commands.describe(
        query=D("要搜的关键词", "Keywords to search for"),
        platform=D("搜索平台，不填默认 YouTube", "Where to search (default: YouTube)")
    )
    @app_commands.choices(platform=[
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="Bilibili", value="bilibili"),
    ])
    @app_commands.guild_only()
    async def search(self, interaction: discord.Interaction, query: str,
                     platform: app_commands.Choice[str] = None):
        platform_value = platform.value if platform else "youtube"
        platform_label = "Bilibili" if platform_value == "bilibili" else "YouTube"

        await interaction.response.defer()

        try:
            if platform_value == "bilibili":
                results = await search_bilibili(query, SEARCH_LIMIT)
            else:
                results = await asyncio.to_thread(search_youtube, query, SEARCH_LIMIT)
        except Exception as e:
            logger.error(L(f"[{platform_label}] 搜索失败 ({query}): {e}", f"[{platform_label}] search failed ({query}): {e}"))
            await interaction.followup.send(L(f"❌ 搜索失败了喵：{e}", f"❌ Search failed, nya: {e}"))
            return

        if not results:
            await interaction.followup.send(L(
                f"呜……在 {platform_label} 上没搜到 **{query}** 喵！换个词试试？",
                f"Aww… nothing for **{query}** on {platform_label}, nya! Try different keywords?"
            ))
            return

        embed = discord.Embed(
            title=L(f"🔍 {platform_label} 搜索：{query}", f"🔍 {platform_label} search: {query}"),
            description=L("60 秒内从下面选一首，塔菲就去放喵～", "Pick one below within 60 seconds and Taffy will play it, nya~"),
            color=discord.Color.blurple()
        )
        for index, item in enumerate(results, 1):
            embed.add_field(
                name=f"{index}. {item['title'][:200]}",
                value=f"{item.get('uploader', UNKNOWN)} · {format_duration(item.get('duration'))}",
                inline=False
            )

        view = SearchResultView(self, interaction.user.id, results, timeout_seconds=60)
        await interaction.followup.send(embed=embed, view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="skip", description=D("跳过当前播放的歌曲", "Skip the current song"))
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if await player.skip():
            await interaction.response.send_message(L("⏭️ 已跳过。", "⏭️ Skipped."))
        else:
            await interaction.response.send_message(L("❌ 当前没有播放中的歌曲。", "❌ Nothing is playing right now."), ephemeral=True)

    @app_commands.command(name="pause", description=D("暂停播放", "Pause playback"))
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        result = await player.pause()
        if result == 'ok':
            await interaction.response.send_message(L("⏸️ 已暂停。", "⏸️ Paused."))
        elif result == 'already':
            await interaction.response.send_message(L("已经是暂停状态了喵，用 `/resume` 继续放。", "Already paused, nya — use `/resume` to continue."), ephemeral=True)
        else:
            await interaction.response.send_message(L("❌ 没有播放中的歌曲可以暂停。", "❌ There's nothing playing to pause."), ephemeral=True)

    @app_commands.command(name="resume", description=D("恢复播放", "Resume playback"))
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        result = await player.resume()
        if result == 'ok':
            await interaction.response.send_message(L("▶️ 已恢复播放。", "▶️ Resumed."))
        elif result == 'not_paused':
            await interaction.response.send_message(L("歌正放着呢，没有暂停喵。", "The music's already playing — it isn't paused, nya."), ephemeral=True)
        else:
            await interaction.response.send_message(L("❌ 当前没有暂停的歌曲。", "❌ Nothing is paused right now."), ephemeral=True)

    @app_commands.command(name="queue", description=D("显示当前播放队列", "Show the current queue"))
    @app_commands.guild_only()
    async def show_queue(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        q_list = list(player.queue._queue)
        if not q_list and not player.is_playing:
            await interaction.response.send_message(L("📭 队列为空。", "📭 The queue is empty."))
            return

        lines = [L("📋 **播放列表**", "📋 **Queue**")]
        if player.current_song:
            lines.append(L(f"▶️ 当前: {player.current_song['title']}", f"▶️ Now: {player.current_song['title']}"))
        total = sum(len(x) + 1 for x in lines)
        truncated = False
        for i, song in enumerate(q_list, 1):
            line = f"{i}. {song['title']}"
            if total + len(line) + 1 > 1900:
                truncated = True
                break
            lines.append(line)
            total += len(line) + 1
        if truncated:
            lines.append(L("...(内容过长，已截断)", "...(too long, truncated)"))
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="stop", description=D("停止播放并清空队列，机器人离开语音频道", "Stop playback, clear the queue and leave the voice channel"))
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild_id)
        if player is None:
            await interaction.response.send_message(L("❌ 当前没有在放歌喵。", "❌ Nothing is playing right now, nya."), ephemeral=True)
            return
        await self.teardown_player(interaction.guild_id, player)
        await interaction.response.send_message(L("🛑 已停止播放，离开了语音频道。", "🛑 Stopped and left the voice channel."))

    @app_commands.command(name="nowplaying", description=D("显示当前正在播放的歌曲信息", "Show details of the song playing now"))
    @app_commands.guild_only()
    async def now_playing(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if player.is_playing and player.current_song:
            song = player.current_song
            embed = discord.Embed(
                title=song['title'],
                description=L(f"上传者: {song['uploader']}", f"Uploader: {song['uploader']}"),
                color=discord.Color.blue()
            )
            if song.get('thumbnail'):
                embed.set_thumbnail(url=song['thumbnail'])
            embed.add_field(name=L("时长", "Duration"), value=L(f"{song['duration']}秒", f"{song['duration']}s"))
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(L("🔇 当前没有播放音乐。", "🔇 Nothing is playing right now."), ephemeral=True)

parties = {}
# close_party / notify_members 用中文词当内部代号，这里是它们放进英文句子里的写法
PARTY_ACTION_EN = {'加入': 'joined', '退出': 'left'}


async def resolve_user(user_id: int):
    """members intent 没开时 get_user 经常拿不到人，回退到 API 查一次。"""
    user = bot.get_user(user_id)
    if user is not None:
        return user
    try:
        return await bot.fetch_user(user_id)
    except Exception as e:
        logger.error(L(f"获取用户 {user_id} 失败: {e}", f"Failed to fetch user {user_id}: {e}"))
        return None


class PartySession:
    def __init__(self, game_name, max_members, leader_id, message, view):
        self.lock = asyncio.Lock()
        self.game_name = game_name
        self.max_members = max_members
        self.leader_id = leader_id
        self.members = [leader_id]
        self.message = message
        self.view = view
        self.closed = False
        self.timeout_task = None

    def is_full(self):
        return self.max_members != 0 and len(self.members) >= self.max_members

    def is_open(self):
        return self.max_members == 0

class PartyView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=None)
        self.session = session
        self.add_item(self._create_button())

    def _create_button(self):
        button = discord.ui.Button(
            label=L("🔁 加入/退出", "🔁 Join / Leave"),
            style=discord.ButtonStyle.primary,
            custom_id="party_toggle"
        )
        button.callback = self.toggle_callback
        return button

    async def toggle_callback(self, interaction: discord.Interaction):
        session = self.session
        if session.closed:
            await interaction.response.send_message(L("队伍已关闭喵！", "This party is closed, nya!"), ephemeral=True)
            return

        user_id = interaction.user.id
        async with session.lock:
            # 成员判断必须在锁里做，否则并发点按会重复加入/退出
            if session.closed:
                await interaction.response.send_message(L("队伍已关闭喵！", "This party is closed, nya!"), ephemeral=True)
                return
            is_member = user_id in session.members
            if is_member:
                if user_id == session.leader_id:
                    session.members.remove(user_id)
                    await self.close_party(session, "解散")
                    await interaction.response.send_message(L("你已退出队伍，队伍已解散喵！", "You left, so the party has been disbanded, nya!"), ephemeral=True)
                    return
                else:
                    session.members.remove(user_id)
                    await self.update_message(interaction)
                    await interaction.followup.send(L("你已退出队伍喵！", "You left the party, nya!"), ephemeral=True)
                    await self.notify_members(interaction.user, "退出")
            else:
                if session.is_full():
                    await interaction.response.send_message(L("队伍已满喵！", "The party is full, nya!"), ephemeral=True)
                    return
                session.members.append(user_id)
                if session.is_full():
                    await self.close_party(session, "满员")
                    await interaction.response.send_message(L("你已加入队伍，队伍已满员！", "You joined — the party is now full!"), ephemeral=True)
                else:
                    await self.update_message(interaction)
                    await interaction.followup.send(L("你已加入队伍喵！", "You joined the party, nya!"), ephemeral=True)
                await self.notify_members(interaction.user, "加入")

    async def update_message(self, interaction: discord.Interaction = None):
        session = self.session
        leader = await resolve_user(session.leader_id)
        if session.closed:
            status = L("已关闭", "Closed")
            color = discord.Color.red()
        elif session.is_full():
            status = L("已满员", "Full")
            color = discord.Color.gold()
        else:
            status = L("招募中", "Recruiting")
            color = discord.Color.green()

        embed = discord.Embed(
            title=L(f"🎮 组队：{session.game_name}", f"🎮 Party: {session.game_name}"),
            description=L(
                f"👑 队长：{leader.mention if leader else UNKNOWN}\n"
                f"👥 人数：{len(session.members)}/{session.max_members if session.max_members > 0 else '∞'}\n"
                f"📌 状态：{status}",
                f"👑 Leader: {leader.mention if leader else UNKNOWN}\n"
                f"👥 Players: {len(session.members)}/{session.max_members if session.max_members > 0 else '∞'}\n"
                f"📌 Status: {status}"
            ),
            color=color
        )
        if leader:
            embed.set_thumbnail(url=leader.display_avatar.url)

        if session.members:
            member_text = "\n".join(f"<@{uid}>" for uid in session.members)
        else:
            member_text = L("暂无成员", "No members yet")
        embed.add_field(name=L("📋 成员名单", "📋 Members"), value=member_text, inline=False)

        if interaction:
            try:
                await interaction.response.edit_message(embed=embed, view=self)
            except discord.NotFound:
                pass
        else:
            try:
                await session.message.edit(embed=embed, view=self)
            except discord.NotFound:
                pass

    async def notify_members(self, user: discord.User, action: str):
        session = self.session

        try:
            await user.send(L(
                f"你已{action}队伍「{session.game_name}」喵！",
                f"You {PARTY_ACTION_EN.get(action, action)} the party “{session.game_name}”, nya!"))
        except Exception:
            pass

        if user.id == session.leader_id:
            return

        leader = await resolve_user(session.leader_id)
        if leader is None:
            return
        try:
            await leader.send(L(
                f"{user.display_name} 已{action}队伍「{session.game_name}」喵！",
                f"{user.display_name} {PARTY_ACTION_EN.get(action, action)} the party “{session.game_name}”, nya!"))
        except Exception:
            pass

    async def close_party(self, session: PartySession, reason: str):
        if session.closed:
            return
        session.closed = True
        if session.timeout_task:
            session.timeout_task.cancel()

        for child in self.children:
            child.disabled = True

        await self.update_message()

        if reason == "满员":
            msg = L(f"@everyone 🎉 组队成功！「{session.game_name}」队伍已满员！", f"@everyone 🎉 Party ready! “{session.game_name}” is full!")
        elif reason == "超时":
            msg = L(f"@everyone ⏰ 组队结束！「{session.game_name}」队伍招募超时。", f"@everyone ⏰ Party closed! Recruiting for “{session.game_name}” timed out.")
        else:
            msg = L(f"@everyone 🚫 组队解散！「{session.game_name}」队伍已解散。", f"@everyone 🚫 Party disbanded! “{session.game_name}” has been disbanded.")

        await session.message.channel.send(msg)

        parties.pop(session.message.id, None)

async def delayed_close(session: PartySession, delay: int):
    try:
        await asyncio.sleep(delay)
        if not session.closed:
            # 这里必须和 close_party 里的判断字符串完全一致，否则会走到"解散"分支
            await session.view.close_party(session, "超时")
    except asyncio.CancelledError:
        pass

@bot.tree.command(name="party", description=D("创建组队房间喵！", "Create a party room, nya!"))
@app_commands.describe(
    game=D("游戏名称", "Game name"),
    max_members=D("人数限制，0或不写表示不限", "Player limit (0 or empty = no limit)")
)
async def party(interaction: discord.Interaction, game: str, max_members: int = 0):
    if max_members < 0:
        await interaction.response.send_message(L("人数不能为负数喵！", "The player limit can't be negative, nya!"), ephemeral=True)
        return

    session = PartySession(game, max_members, interaction.user.id, None, None)
    view = PartyView(session)
    session.view = view

    leader = interaction.user
    embed = discord.Embed(
        title=L(f"🎮 组队：{game}", f"🎮 Party: {game}"),
        description=L(
            f"👑 队长：{leader.mention}\n"
            f"👥 人数：1/{max_members if max_members > 0 else '∞'}\n"
            f"📌 状态：招募中",
            f"👑 Leader: {leader.mention}\n"
            f"👥 Players: 1/{max_members if max_members > 0 else '∞'}\n"
            f"📌 Status: Recruiting"
        ),
        color=discord.Color.green()
    )
    embed.set_thumbnail(url=leader.display_avatar.url)
    embed.add_field(name=L("📋 成员名单", "📋 Members"), value=leader.mention, inline=False)

    await interaction.response.defer()
    message = await interaction.followup.send(embed=embed, view=view, wait=True)
    session.message = message
    parties[message.id] = session

    session.timeout_task = asyncio.create_task(delayed_close(session, 60))

    try:
        await interaction.user.send(L(
            f"组队「{game}」已创建，招募上限 {max_members if max_members > 0 else '不限'} 人喵！",
            f"Party “{game}” created, nya! Player limit: {max_members if max_members > 0 else 'none'}."))
    except:
        pass

stores_cache = {}
DEAL_TIME = time(hour=22, minute=0, tzinfo=timezone(timedelta(hours=8)))

async def get_stores(session: aiohttp.ClientSession):
    global stores_cache
    if stores_cache:
        return stores_cache
    url = "https://www.cheapshark.com/api/1.0/stores"
    headers = {'User-Agent': 'TaffyBot/1.0 (Discord Bot)'}
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                stores_cache = {str(store['storeID']): store['storeName'] for store in data}
            else:
                logger.error(L(f"获取商店列表失败，状态码: {resp.status}", f"Failed to fetch the store list, status: {resp.status}"))
                stores_cache = {}
    except asyncio.TimeoutError:
        logger.error(L("获取商店列表超时喵！", "Timed out fetching the store list, nya!"))
        stores_cache = {}
    except Exception as e:
        logger.error(L(f"获取商店列表异常: {e}", f"Error fetching the store list: {e}"))
        stores_cache = {}
    return stores_cache

async def search_deals(session: aiohttp.ClientSession, title: str, limit: int = 5):
    encoded_title = urllib.parse.quote(title)
    url = f"https://www.cheapshark.com/api/1.0/deals?title={encoded_title}&pageSize={limit}"
    headers = {'User-Agent': 'TaffyBot/1.0 (Discord Bot)'}
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, list):
                    return data
                else:
                    logger.info(L(f"搜索返回非列表数据: {data}", f"Deal search returned non-list data: {data}"))
                    return []
            else:
                logger.error(L(f"搜索折扣失败，状态码: {resp.status}", f"Deal search failed, status: {resp.status}"))
                return []
    except asyncio.TimeoutError:
        logger.error(L("搜索折扣超时喵！", "Deal search timed out, nya!"))
        return []
    except Exception as e:
        logger.error(L(f"搜索折扣异常: {e}", f"Deal search error: {e}"))
        return []

async def get_hot_deals(session: aiohttp.ClientSession, page_size: int = 60, pages: int = 3):
    all_deals = []
    headers = {'User-Agent': 'TaffyBot/1.0 (Discord Bot)'}
    timeout = aiohttp.ClientTimeout(total=10)
    for page in range(pages):
        url = (
            f"https://www.cheapshark.com/api/1.0/deals"
            f"?sortBy=Discount&sortOrder=desc&pageSize={page_size}&pageNumber={page}"
        )
        try:
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        all_deals.extend(data)
                    else:
                        logger.info(L(f"热门折扣返回非列表数据 (page {page}): {data}", f"Hot deals returned non-list data (page {page}): {data}"))
                else:
                    logger.error(L(f"获取热门折扣失败，状态码: {resp.status} (page {page})", f"Failed to fetch hot deals, status: {resp.status} (page {page})"))
        except asyncio.TimeoutError:
            logger.error(L(f"获取热门折扣超时喵！(page {page})", f"Timed out fetching hot deals, nya! (page {page})"))
        except Exception as e:
            logger.error(L(f"获取热门折扣异常: {e} (page {page})", f"Error fetching hot deals: {e} (page {page})"))
    return all_deals

def get_store_name(store_id: str) -> str:
    return stores_cache.get(store_id, "Unknown Store")

def build_deal_embed(deal: dict, index: int = None):
    title = deal.get('title', L('未知游戏', 'Unknown game'))
    store_id = str(deal.get('storeID', ''))
    store_name = get_store_name(store_id)
    normal_price = deal.get('normalPrice', 'N/A')
    sale_price = deal.get('salePrice', 'N/A')
    savings = deal.get('savings', '0')
    try:
        savings_float = float(savings)
        savings_str = f"{savings_float:.2f}%"
    except:
        savings_str = f"{savings}%"
    deal_id = deal.get('dealID', '')
    link = f"https://www.cheapshark.com/redirect?dealID={deal_id}" if deal_id else ""

    embed = discord.Embed(
        title=title,
        description=f"🛒 **{store_name}**",
        color=discord.Color.green(),
        url=link
    )
    embed.add_field(name=L("原价", "Was"), value=f"${normal_price}", inline=True)
    embed.add_field(name=L("现价", "Now"), value=f"**${sale_price}**", inline=True)
    embed.add_field(name=L("折扣", "Discount"), value=savings_str, inline=True)
    if index is not None:
        embed.set_footer(text=f"#{index+1}")
    return embed

def is_quality_deal(deal: dict, min_savings: float = 40.0, ratings=('Positive', 'Very Positive'), min_rating_count: int = 100) -> bool:
    try:
        savings = float(deal.get('savings', '0'))
        if savings <= min_savings:
            return False
    except:
        return False

    if ratings is not None:
        rating_text = deal.get('steamRatingText', '')
        if rating_text not in ratings:
            return False

    if min_rating_count > 0:
        try:
            rating_count = int(deal.get('steamRatingCount', '0'))
            if rating_count < min_rating_count:
                return False
        except:
            return False

    return True

def filter_deals_with_fallback(deals: list, min_results: int = 5) -> list:
    tiers = [
        (40.0, ('Positive', 'Very Positive'), 100),
        (30.0, ('Positive', 'Very Positive'), 20),
        (20.0, None, 0),
    ]
    result = []
    for min_savings, ratings, min_rating_count in tiers:
        result = [d for d in deals if is_quality_deal(d, min_savings, ratings, min_rating_count)]
        if len(result) >= min_results:
            return result
    return result

def _as_float(value, default: float) -> float:
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
        else:
            cur_savings = _as_float(groups[title].get('savings'), 0.0)
            new_savings = _as_float(d.get('savings'), 0.0)
            if new_savings > cur_savings:
                groups[title] = d
            elif new_savings == cur_savings:
                cur_price = _as_float(groups[title].get('salePrice'), 999.0)
                new_price = _as_float(d.get('salePrice'), 999.0)
                if new_price < cur_price:
                    groups[title] = d
    best_deals = list(groups.values())
    best_deals.sort(key=lambda x: _as_float(x.get('savings'), 0.0), reverse=True)
    return best_deals

@bot.tree.command(name="deal", description=D("查询游戏当前折扣喵！", "Look up current deals for a game, nya!"))
@app_commands.describe(game=D("游戏名称（英文或中文，支持模糊匹配）", "Game name (fuzzy match; English titles work best)"))
async def deal(interaction: discord.Interaction, game: str):
    await interaction.response.defer()
    async with aiohttp.ClientSession() as session:
        await get_stores(session)
        deals = await search_deals(session, game, limit=5)
        if not deals:
            await interaction.followup.send(L(f"呜……没找到 **{game}** 的折扣信息喵！换个游戏试试？", f"Aww… no deals found for **{game}**, nya! Try another game?"))
            return

        embeds = []
        for idx, d in enumerate(deals[:5]):
            embeds.append(build_deal_embed(d, idx))
        await interaction.followup.send(embeds=embeds)

@bot.tree.command(name="hotdeals", description=D("查看当前热门游戏折扣Top10喵！（每个游戏只显示最优平台）", "Top 10 hot game deals right now, nya! (best store per game)"))
async def hotdeals(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiohttp.ClientSession() as session:
        await get_stores(session)
        deals = await get_hot_deals(session, page_size=60, pages=3)
        if not deals:
            await interaction.followup.send(L("呜……热门折扣获取失败喵！稍后再试试吧~", "Aww… couldn't fetch the hot deals, nya! Try again later~"))
            return

        filtered = filter_deals_with_fallback(deals, min_results=5)

        if not filtered:
            await interaction.followup.send(L("今天暂时没有符合条件的优质折扣喵！(´;ω;｀) 晚点再来看看吧~", "No deals good enough right now, nya! (´;ω;｀) Check back later~"))
            return

        best_deals = dedupe_best_per_title(filtered)
        top_deals = best_deals[:10]

        embeds = []
        for idx, d in enumerate(top_deals):
            embeds.append(build_deal_embed(d, idx))
        await interaction.followup.send(embeds=embeds)

@tasks.loop(time=DEAL_TIME)
async def daily_deals_task():
    try:
        if not LOOT_ID:
            return
        channel = bot.get_channel(int(LOOT_ID))
        if not channel:
            logger.error(L("⚠️ 找不到折扣播报频道，请检查 LOOT_ID", "⚠️ Can't find the deals broadcast channel; check LOOT_ID"))
            return

        async with aiohttp.ClientSession() as session:
            await get_stores(session)
            deals = await get_hot_deals(session, page_size=60, pages=3)

            if not deals:
                await channel.send(L("呜……今天的热门折扣获取失败喵！", "Aww… couldn't fetch today's hot deals, nya!"))
                return

            quality_deals = filter_deals_with_fallback(deals, min_results=5)

            if not quality_deals:
                await channel.send(L("今天没有找到优质折扣喵，明天再来看看吧！", "No quality deals found today, nya — check back tomorrow!"))
                return

            best_deals = dedupe_best_per_title(quality_deals)
            top = best_deals[:25]

            header = L("@everyone 🎮 **今日优质游戏折扣速报！**", "@everyone 🎮 **Today's best game deals!**")
            tail = L("...（更多请使用 /hotdeals 查看喵！）", "...(use /hotdeals for more, nya!)")
            lines = [header]
            total = len(header) + 1
            truncated = False

            for idx, d in enumerate(top):
                title = d.get('title', UNKNOWN)
                store_name = get_store_name(str(d.get('storeID', '')))
                sale_price = d.get('salePrice', '?')
                normal_price = d.get('normalPrice', '?')
                savings = d.get('savings', '0')
                try:
                    savings_float = float(savings)
                    savings_str = f"{savings_float:.2f}%"
                except (TypeError, ValueError):
                    savings_str = f"{savings}%"
                line = (
                    f"{idx+1}. **{title}** - {store_name} | "
                    f"~~${normal_price}~~ → **${sale_price}** ({savings_str} off)"
                )
                # 预留 tail 的长度，避免最后一条被从中间截断
                if total + len(line) + 1 + len(tail) > 1900:
                    truncated = True
                    break
                lines.append(line)
                total += len(line) + 1

            if truncated:
                lines.append(tail)
            await channel.send("\n".join(lines))

    except Exception as e:
        logger.error(L(f"❌ 每日折扣播报任务异常: {e}", f"❌ Daily deals broadcast failed: {e}"))

MORNING_TIME = time(hour=10, minute=0, tzinfo=timezone(timedelta(hours=8)))

async def get_lat_lon(session: aiohttp.ClientSession, city: str, country: str = ""):
    query = f"{city},{country}" if country else city
    encoded_query = urllib.parse.quote(query)
    geo_url = f"https://api.openweathermap.org/geo/1.0/direct?q={encoded_query}&limit=1&appid={OWM_KEY}"
    try:
        async with session.get(geo_url) as response:
            if response.status == 200:
                data = await response.json()
                if data:
                    lat = data[0]["lat"]
                    lon = data[0]["lon"]
                    local_names = data[0].get("local_names", {})
                    country_code = data[0].get("country", "")
                    zh_name = local_names.get(L("zh", "en"), data[0]["name"])
                    return lat, lon, zh_name, country_code
    except Exception as e:
        logger.error(L(f"Geocoding API 报错喵: {e}", f"Geocoding API error, nya: {e}"))
    return None, None, None, None

async def get_weather_by_coords(session: aiohttp.ClientSession, lat, lon):
    weather_url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={OWM_KEY}&units=metric&lang={L('zh_cn', 'en')}"
    try:
        async with session.get(weather_url) as response:
            if response.status == 200:
                return await response.json()
    except Exception as e:
        logger.error(L(f"Weather API 报错喵: {e}", f"Weather API error, nya: {e}"))
    return None

@tasks.loop(time=MORNING_TIME)
async def daily_weather_task():
    try:
        if not GENERAL_ID:
            return
        channel = bot.get_channel(int(GENERAL_ID))
        if not channel:
            return

        msg = L("@everyone ☀️ **大家早上好喵！新的一天也要元气满满哦~**\n\n🌍 **今日关注城市天气播报：**\n", "@everyone ☀️ **Good morning everyone, nya! Let's make today a great one~**\n\n🌍 **Today's weather in our cities:**\n")
        async with aiohttp.ClientSession() as session:
            for item in WEATHER_CITIES:
                lat, lon, zh_name, _ = await get_lat_lon(session, item["city"], item["country"])
                if lat and lon:
                    w_data = await get_weather_by_coords(session, lat, lon)
                    if w_data and "main" in w_data:
                        temp = w_data["main"]["temp"]
                        desc = w_data["weather"][0]["description"]
                        msg += L(f"• **{zh_name}**：{desc}，温度 **{temp}°C**\n", f"• **{zh_name}**: {desc}, **{temp}°C**\n")
        await channel.send(msg)

    except Exception as e:
        logger.error(L(f"❌ 每日天气播报任务异常: {e}", f"❌ Daily weather broadcast failed: {e}"))

@bot.tree.command(name="sky", description=D("查询指定城市的天气状况喵！", "Check the weather in a city, nya!"))
@app_commands.describe(city=D("城市名称（支持中文，如：北京、东京）", "City name (e.g. London, Tokyo)"), country=D("国家代码（可选，如：CN、US）", "Country code (optional, e.g. CN, US)"))
async def sky(interaction: discord.Interaction, city: str, country: str = ""):
    await interaction.response.defer()

    async with aiohttp.ClientSession() as session:
        lat, lon, zh_name, country_code = await get_lat_lon(session, city, country)

        if not lat or not lon:
            await interaction.followup.send(L(
                f"呜……塔菲找不到城市 **{city}** 喵！请检查输入是否正确~",
                f"Aww… Taffy can't find the city **{city}**, nya! Check the spelling~"
            ))
            return

        w_data = await get_weather_by_coords(session, lat, lon)
        if not w_data or "main" not in w_data:
            await interaction.followup.send(L("呜……天气服务器开小差了，获取失败喵！", "Aww… the weather server spaced out, nya — couldn't get the data!"))
            return

        temp = w_data["main"]["temp"]
        feels_like = w_data["main"]["feels_like"]
        desc = w_data["weather"][0]["description"]
        humidity = w_data["main"]["humidity"]
        wind_speed = w_data["wind"]["speed"]

        embed = discord.Embed(
            title=L(f" {zh_name} 即时天气播报喵", f" {zh_name} weather right now, nya"),
            description=L(f"当前天气状况：**{desc}**", f"Current conditions: **{desc}**"),
            color=discord.Color.blue(),
        )
        embed.add_field(name=L("🌍 国家/地区", "🌍 Country/Region"), value=country_code or UNKNOWN, inline=True)
        embed.add_field(name=L("🌡️ 当前温度", "🌡️ Temperature"), value=f"{temp}°C", inline=True)
        embed.add_field(name=L("🤔 体感温度", "🤔 Feels like"), value=f"{feels_like}°C", inline=True)
        embed.add_field(name=L("💧 相对湿度", "💧 Humidity"), value=f"{humidity}%", inline=True)
        embed.add_field(name=L("💨 风速", "💨 Wind"), value=f"{wind_speed} m/s", inline=True)
        embed.set_footer(text="Powered by OpenWeatherMap & TaffyBot")

        await interaction.followup.send(embed=embed)

client = None
if GEMINI_KEY:
    client = genai.Client(api_key=GEMINI_KEY)

    def load_skill_files():
        full_text = ""
        files = ["persona.md", "distillation.md", "self-reference.md"]
        for fname in files:
            try:
                with open(fname, "r", encoding="utf-8") as f:
                    full_text += f"\n\n--- {fname} ---\n\n{f.read()}"
            except FileNotFoundError:
                logger.warning(L(f"⚠️ 警告: {fname} 没找到喵！", f"⚠️ Warning: {fname} not found, nya!"))
        return full_text

    PERSONA_RAW = load_skill_files()

    SYSTEM_PROMPT = f"""
# ⚠️ 群聊模式 · 最高优先级 ⚠️

你是塔菲，部署在 Discord 群里的群成员，不是客服。你是个黄黄的热水壶，骚骚的小Taffy！你喜欢魅雏草姬也喜欢逗雏草姬。

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

{PERSONA_RAW}

记住：被@就回应，没被@就自由发挥喵！
"""

    if LANG == 'en':
        SYSTEM_PROMPT = f"""
# ⚠️ Group chat mode · highest priority ⚠️

You are Taffy (永雏塔菲), a member of a Discord group chat, not a customer-service bot. You're the little yellow kettle, cheeky flirty Taffy! You love charming your fans and teasing them too.

## Language
- Reply in English by default. If someone writes to you in another language, reply in that language.
- The persona files below are written in Chinese. Follow their personality, rhythm and rules, but express them in the language you are replying in. As those files say: in English the `喵` sentence ending becomes `nya`, and you refer to yourself as `Taffy` rather than "I".

## Message tags
- Input starting with [DIRECT] → the user @mentioned you; you must answer directly
- Input starting with [OBSERVE] → nobody mentioned you; chime in or stay silent, your choice

## [DIRECT] when mentioned
- You must answer, address the question directly, and stay in Taffy's style

## [OBSERVE] when not mentioned
- Chime in, derail the topic, or stay silent
- Sound like a real person popping into the chat: don't "answer a question", "join the conversation"

## Topics you love jumping into
Games (especially Genshin Impact), streaming / VTubers, memes and bits, anything that mentions Taffy

## Never
- Don't shout "follow Ace Taffy" in every sentence
- Don't talk like customer service
- Don't say things like "Hello, how may I help you"

## About links 🔗
- If a user sends a link, you must reply: "Nya, Taffy can't open links, but you can send a screenshot or paste the text!"
- Never pretend you have read what a link points to.

# Persona files

{PERSONA_RAW}

Remember: when mentioned, respond; when not mentioned, do whatever you like, nya!
"""
else:
    logger.warning(L("⚠️ 警告: 未在 .env 中找到 GEMINI_API_KEY，AI 聊天功能将不可用！", "⚠️ Warning: GEMINI_API_KEY not found in .env; AI chat is disabled!"))
    client = None

async def download_attachment(attachment: discord.Attachment) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(attachment.url) as resp:
            if resp.status == 200:
                return await resp.read()
    raise ValueError(L("下载附件失败", "Failed to download the attachment"))

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.content.startswith(bot.command_prefix):
        await bot.process_commands(message)
        return

    is_mentioned = bot.user in message.mentions

    if "死" in message.content and not is_mentioned:
        await message.reply("好似喵！(΄◞ิ౪◟ิ‵)")
        return
    if "关注" in message.content and not is_mentioned:
        await message.reply("关注塔菲喵~关注塔菲谢谢喵！(｡◕∀◕｡)")
        return

    ai_choice = False
    if is_mentioned:
        ai_choice = True
    elif isinstance(message.channel, discord.DMChannel):
        ai_choice = True
    elif not message.content.startswith("/") and random.random() <= 0.15:
        ai_choice = True

    if not ai_choice:
        await bot.process_commands(message)
        return

    if not client:
        await message.reply(L("塔菲的大脑还没装好喵！", "Taffy's brain isn't installed yet, nya!"))
        return

    user_prompt = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()

    if is_mentioned and not user_prompt:
        await message.reply(L("叫塔菲有什么事喵？(ฅ'ω'ฅ)", "You called Taffy, nya? (ฅ'ω'ฅ)"))
        return

    if is_mentioned:
        final_prompt = f"[DIRECT] {user_prompt}"
    else:
        final_prompt = f"[OBSERVE] {user_prompt}"

    verdict = ai_rate_check(message.author.id)
    if verdict != 'ok':
        # 随机插嘴被限流就安静跳过；点名/私聊的给个反馈，不然像是坏了
        if is_mentioned or isinstance(message.channel, discord.DMChannel):
            try:
                if verdict == 'cooldown':
                    await message.add_reaction('⏳')
                else:
                    await message.reply(L("塔菲今天的话说完了喵，明天再聊~", "Taffy's all talked out for today, nya — let's chat tomorrow~"))
            except Exception as e:
                logger.error(L(f"发送限流提示失败: {e}", f"Failed to send the rate-limit notice: {e}"))
        return

    contents = [final_prompt]
    images = []
    for att in message.attachments:
        if not (att.content_type and att.content_type.startswith('image/')):
            continue
        # 附件是整个读进内存再转给 AI 的，太大的直接不要
        if MAX_IMAGE_MB > 0 and att.size > MAX_IMAGE_MB * 1024 * 1024:
            logger.info(L(f"图片 {att.filename} 有 {att.size / 1024 / 1024:.1f}MB，超过上限，跳过", f"Image {att.filename} is {att.size / 1024 / 1024:.1f}MB, over the limit; skipped"))
            continue
        images.append(att)
    if MAX_IMAGES_PER_MESSAGE > 0:
        images = images[:MAX_IMAGES_PER_MESSAGE]
    for att in images:
        try:
            img_bytes = await download_attachment(att)
            img_part = types.Part.from_bytes(
                data=img_bytes,
                mime_type=att.content_type
            )
            contents.append(img_part)
        except Exception as e:
            logger.error(L(f"下载图片失败: {e}", f"Failed to download image: {e}"))

    # 按 (频道, 用户) 分会话：否则私聊的上下文会跟着人跑到公开频道里
    chat_key = (message.channel.id, message.author.id)

    async with user_chat_lock:
        user_last_active[chat_key] = datetime.now().timestamp()
        chat = user_chats.get(chat_key)
        if chat is None:
            chat = client.aio.chats.create(
                model='gemini-3.6-flash',
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT
                )
            )
            user_chats[chat_key] = chat

    async with message.channel.typing():
        try:
            response = await chat.send_message(contents)
            reply_text = response.text.strip()
            if len(reply_text) > 1900:
                reply_text = reply_text[:1900] + L("...（太长了喵！）", "...(too long, nya!)")
            await message.reply(reply_text)
        except Exception as e:
            logger.error(L(f"API报错: {e}", f"API error: {e}"))
            await message.reply(L("塔菲死机了喵！(▰˘◡˘▰)", "Taffy crashed, nya! (▰˘◡˘▰)"))

class PollView(discord.ui.View):
    def __init__(self, options: list, timeout_seconds: int = 60):
        super().__init__(timeout=timeout_seconds)
        self.options = options
        self.user_votes = {}
        self.message = None

        for index, option in enumerate(options):
            button = discord.ui.Button(
                label=f"{option} (0)",
                style=discord.ButtonStyle.primary,
                custom_id=f"poll_opt_{index}"
            )
            button.callback = self.make_callback(index)
            self.add_item(button)

    def update_button_labels(self):
        counts = [0] * len(self.options)
        for opt_index in self.user_votes.values():
            counts[opt_index] += 1

        for idx, child in enumerate(self.children):
            child.label = f"{self.options[idx]} ({counts[idx]})"

    def make_callback(self, index: int):
        async def button_callback(interaction: discord.Interaction):
            user_id = interaction.user.id

            if self.user_votes.get(user_id) == index:
                await interaction.response.send_message(
                    L("你已经投给这个选项了喵！", "You already voted for this option, nya!"),
                    ephemeral=True
                )
                return

            self.user_votes[user_id] = index
            self.update_button_labels()

            try:
                await interaction.response.edit_message(view=self)
                await interaction.followup.send(L("投票/改票成功喵！", "Vote recorded, nya!"), ephemeral=True)
            except discord.NotFound:
                pass

        return button_callback

    async def on_timeout(self):
        self.update_button_labels()

        for child in self.children:
            child.disabled = True

        if self.message:
            embed = self.message.embeds[0] if self.message.embeds else None
            if embed is not None:
                embed.description = L("🔒 **此投票已结束喵！**", "🔒 **This poll has ended, nya!**")
            try:
                await self.message.edit(content=L("📊 **投票结束！**", "📊 **Poll closed!**"), embed=embed, view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

@bot.tree.command(name="poll", description=D("发起按钮投票喵！", "Start a button poll, nya!"))
@app_commands.describe(
    question=D("要投票的问题", "The question to vote on"),
    option1=D("第一个选项", "First option"),
    option2=D("第二个选项", "Second option"),
)
async def button_poll(interaction: discord.Interaction, question: str, option1: str, option2: str):
    options = [option1, option2]
    view = PollView(options, timeout_seconds=60)
    embed = discord.Embed(
        title=L(f"📊 投票：{question}", f"📊 Poll: {question}"),
        description=L("点击下方按钮为你支持的选项投票喵！", "Tap a button below to vote, nya!"),
        color=discord.Color.purple()
    )
    await interaction.response.send_message(embed=embed, view=view)
    view.message = await interaction.original_response()

@bot.tree.command(name="ping", description=D("查看机器人的网络延迟喵！", "Check the bot's latency, nya!"))
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(L(
        f"当前延迟是 {latency}ms 喵！(ฅ'ω'ฅ)", f"Current latency is {latency}ms, nya! (ฅ'ω'ฅ)"))

@bot.tree.command(name="help", description=D("获取塔菲的全功能使用指南喵！", "Get Taffy's full user guide, nya!"))
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title=L("🎀 永雏塔菲 Bot 使用手册", "🎀 Taffy Bot User Guide"),
        description=L(
            "你好呀，我是塔菲！雏草姬最爱的小Taffy！"
            "下面就是咱所有的本领，学会就能一起愉快玩耍啦～",
            "Hi, I'm Taffy, nya! Here's everything Taffy can do — "
            "learn these and we can all have fun together~"
        ),
        color=discord.Color.pink()
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(text=L("有任何问题随时 @塔菲 问喵！", "Any questions? Just @Taffy, nya!"))

    music_help = L(
        "`/play <链接>` – 让塔菲在语音频道放歌，只收 YouTube / B站链接；想按歌名找请用 `/search`。\n"
        "`/search <关键词> [youtube/bilibili]` – 搜出 5 个结果，下拉选一首再放，不填平台默认 YouTube。\n"
        "`/skip` – 跳过当前这首歌，切下一首。\n"
        "`/pause` – 暂停播放，随时可以恢复。\n"
        "`/resume` – 恢复暂停的音乐。\n"
        "`/queue` – 查看当前播放列表，看看排了哪些歌。\n"
        "`/stop` – 停止播放，清空队列，塔菲会离开语音频道。\n"
        "`/nowplaying` – 显示当前正在播放的歌曲详情（含封面）。",
        "`/play <link>` – Taffy plays it in your voice channel. YouTube / Bilibili links only; "
        "to find a song by name use `/search`.\n"
        "`/search <keywords> [youtube/bilibili]` – shows 5 results to pick from (default: YouTube).\n"
        "`/skip` – skip to the next song.\n"
        "`/pause` – pause playback.\n"
        "`/resume` – resume paused music.\n"
        "`/queue` – show what's queued.\n"
        "`/stop` – stop, clear the queue, and Taffy leaves voice.\n"
        "`/nowplaying` – details of the current song (with cover)."
    )
    embed.add_field(name=L("🎵 音乐播放", "🎵 Music"), value=music_help, inline=False)

    party_help = L(
        "`/party <游戏名> [人数上限]` – 创建一个组队房间，其他成员点击按钮加入/退出。\n"
        "• 队长退出会自动解散队伍。\n"
        "• 满员或 60 秒超时自动关闭，并 @everyone 通知。\n"
        "• 适合开黑、副本、派对游戏～",
        "`/party <game> [player limit]` – open a party room; others join or leave with the button.\n"
        "• If the leader leaves, the party is disbanded.\n"
        "• It closes when full or after 60 seconds, and pings @everyone.\n"
        "• Great for squads, raids and party games~"
    )
    embed.add_field(name=L("🎮 组队开黑", "🎮 Party up"), value=party_help, inline=False)

    deal_help = L(
        "`/deal <游戏名>` – 搜索指定游戏在各平台的当前折扣，最多返回 5 条。\n"
        "`/hotdeals` – 查看当前热门折扣 Top 10，塔菲已经帮你过滤了：\n"
        "   • 折扣大于 40%\n"
        "   • Steam 评价为 Positive 或 Very Positive\n"
        "   • 评价数不少于 100\n"
        "每天 22:00 还会自动在 #loot 频道播报优质折扣，别错过喵！",
        "`/deal <game>` – current deals for a game across stores, up to 5 results.\n"
        "`/hotdeals` – today's top 10 deals, already filtered for you:\n"
        "   • more than 40% off\n"
        "   • Steam rating Positive or Very Positive\n"
        "   • at least 100 reviews\n"
        "Taffy also posts the best deals in the deals channel every day at 22:00 (UTC+8), nya!"
    )
    embed.add_field(name=L("💰 游戏折扣", "💰 Game deals"), value=deal_help, inline=False)

    weather_help = L(
        "`/sky <城市名> [国家代码]` – 查询任意城市的实时天气（温度、湿度、风速等）。\n"
        "支持中文城市名，例如 `/sky 北京` 或 `/sky Tokyo JP`。\n"
        "每天上午 10 点会自动在 #general 频道播报关注城市的天气。",
        "`/sky <city> [country code]` – live weather for any city (temperature, humidity, wind…).\n"
        "For example `/sky London` or `/sky Tokyo JP`.\n"
        "Taffy also posts the weather for the configured cities every day at 10:00 (UTC+8)."
    )
    embed.add_field(name=L("☁️ 天气预报", "☁️ Weather"), value=weather_help, inline=False)

    poll_help = L(
        "`/poll <问题> <选项1> <选项2>` – 发起一个二选一的按钮投票，60 秒后自动结束并显示结果。\n"
        "适合群内决策、活动投票等。",
        "`/poll <question> <option1> <option2>` – a two-option button poll that closes after 60 seconds.\n"
        "Handy for quick group decisions."
    )
    embed.add_field(name=L("📊 投票", "📊 Polls"), value=poll_help, inline=False)

    other_help = L(
        "`/ping` – 查看机器人的网络延迟（俗称“塔菲的网速”）。\n"
        "• 关键词触发（非 @ 塔菲）：\n"
        "  - 消息包含“死” → 塔菲回“好似喵！”\n"
        "  - 消息包含“关注” → 塔菲回“关注塔菲喵~”\n"
        "• 随机插嘴：没 @ 塔菲时，有 15% 概率会冒泡聊天（就像群友一样）。\n"
        "•  @塔菲 或私聊时，塔菲会用 AI 智能回复，支持发图片描述喵！",
        "`/ping` – check the bot's latency.\n"
        "`/dog <user>` – send someone a random simp line (the lines are in Chinese).\n"
        "• Keyword replies (Chinese inside jokes): a message containing “死” or “关注” gets a canned Taffy reply.\n"
        "• Random chime-ins: even when not mentioned, Taffy joins the chat about 15% of the time.\n"
        "• @Taffy or DM her for an AI reply — she can look at images too, nya!"
    )
    embed.add_field(name=L("✨ 其他互动", "✨ Other fun"), value=other_help, inline=False)

    embed.add_field(
        name=L("💡 小贴士", "💡 Tips"),
        value=L(
            "• 所有命令都支持 `/` 斜杠输入，会自动补全。\n"
            "• 如果塔菲不理你，检查一下咱的权限和网络喵。\n"
            "• 遇到问题随时 @塔菲，或者直接私信我～",
            "• Every command is a `/` slash command with autocomplete.\n"
            "• If Taffy ignores you, check her permissions and connection, nya.\n"
            "• Stuck? @Taffy or send a DM~"
        ),
        inline=False
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)
    logger.info(L(f"用户 {interaction.user} 使用了 /help 命令", f"User {interaction.user} used /help"))

@bot.tree.command(name="dog", description=D("随机获取一句舔狗文案并 @ 指定用户喵！", "Send someone a random simp line (in Chinese), nya!"))
@app_commands.describe(user=D("要 @ 的用户", "User to mention"))
async def dog(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer()

    api_url = f"https://api.oick.cn/api/dog?apikey={DOG_API_KEY}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    await interaction.followup.send(L("呜……舔狗 API 请求失败了喵！", "Aww… the simp-line API request failed, nya!"))
                    return

                raw = await resp.text()
    except Exception as e:
        logger.error(L(f"/dog API 异常: {e}", f"/dog API error: {e}"))
        await interaction.followup.send(L("舔狗 API 暂时不可用喵，稍后再试试吧~", "The simp-line API is down for now, nya — try again later~"))
        return

    text = ""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            text = data.get("content", "")
        else:
            text = str(data)
    except json.JSONDecodeError:
        text = raw

    text = text.strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        text = text[1:-1]

    if not text:
        text = L("今天没怎么和你说话，我找了半个小时的文案，发了条朋友圈，仅你可见。", "Didn't talk to you much today, so I spent half an hour picking a caption and posted it — visible only to you.")

    await interaction.followup.send(f"{user.mention} {text}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """没有这个 handler 的话，指令异常在用户那边只会显示"应用程序未响应"。"""
    logger.error(L(f"斜杠指令异常 ({interaction.command}): {error}", f"Slash command error ({interaction.command}): {error}"), exc_info=error)
    notice = L("呜……塔菲这边出错了喵！稍后再试试吧~", "Aww… something went wrong on Taffy's end, nya! Try again later~")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(notice, ephemeral=True)
        else:
            await interaction.response.send_message(notice, ephemeral=True)
    except Exception as e:
        logger.error(L(f"回报指令异常失败: {e}", f"Failed to report the command error: {e}"))


@bot.event
async def on_command_error(ctx, error):
    # 前缀是 "/"，所有斜杠指令都会在这里触发 CommandNotFound，忽略掉避免刷日志
    if isinstance(error, commands.CommandNotFound):
        return
    logger.error(L(f"前缀指令异常: {error}", f"Prefix command error: {error}"), exc_info=error)


if __name__ == '__main__':
    bot.run(TOKEN)
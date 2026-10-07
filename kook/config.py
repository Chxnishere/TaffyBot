"""集中配置：环境变量、常量、路径。"""
import os
from datetime import timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')

# ---------- 凭证 ----------
TOKEN = (os.getenv('KOOK_TOKEN') or '').strip()  # strip: 粘贴时带上的空格/换行是最常见的连不上原因
GEMINI_KEY = os.getenv('GEMINI_API_KEY')
OWM_KEY = os.getenv('OWM_API_KEY')
DOG_API_KEY = os.getenv('DOG_API_KEY', '')

# ---------- 播报频道（KOOK 频道 id，是字符串） ----------
GENERAL_ID = os.getenv('GENERAL_ID')
LOOT_ID = os.getenv('LOOT_ID')

# ---------- 路径 ----------
# 全部用绝对路径，从别的目录启动也能找到 cookies / 人设文件。
DB_PATH = os.getenv('DB_PATH') or str(BASE_DIR / 'taffybot.db')
COOKIE_PATH = os.getenv('COOKIE_PATH') or str(BASE_DIR / 'cookies.txt')
FFMPEG_LOG_PATH = str(BASE_DIR / 'ffmpeg_debug.log')
SKILLS_DIR = BASE_DIR / 'skills'
LOCK_PATH = os.getenv('LOCK_PATH') or '/tmp/taffybot-kook.lock'

FFMPEG_BIN = os.getenv('FFMPEG_BIN', 'ffmpeg')

# ---------- 时区 ----------
CST = timezone(timedelta(hours=8))

# ---------- 定时 ----------
DEAL_HOUR, DEAL_MINUTE = 22, 0        # 每日折扣播报
MORNING_HOUR, MORNING_MINUTE = 10, 0  # 每日天气播报

# ---------- 会话 / 播放 ----------
USER_CHAT_TIMEOUT = 3600      # AI 上下文多久没动就丢弃
IDLE_TIMEOUT = 300            # 语音闲置多久自动退出
URL_REFRESH_AFTER = 1800      # 直链超过这个秒数就重新解析
SEARCH_LIMIT = 5
FFMPEG_LOG_MAX_BYTES = 10 * 1024 * 1024

# KOOK 的消息长度上限，单独抽出来方便调
MAX_REPLY_CHARS = 4500

# ---------- 数据保留 ----------
DEAL_HISTORY_DAYS = 30
MAX_REMINDER_SECONDS = 365 * 24 * 3600

# ---------- 本地取流中转 ----------
# B 站的 CDN 不带 Referer 会直接 403。
# 所以在 127.0.0.1 上开一个小服务，由它带着正确的 header 去取流，
# 再把字节转给 ffmpeg。详见 voice/relay.py。
RELAY_HOST = '127.0.0.1'
RELAY_PORT = int(os.getenv('RELAY_PORT', '17650'))

BROWSER_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
)

# ---------- 音质 ----------
# 音量倍率。1.0 = 原样输出，觉得吵可以调到 0.7~0.8。
VOICE_VOLUME = float(os.getenv('VOICE_VOLUME', '1.0'))

# 码率。0 = 用 KOOK 给频道分配的值（推荐）。
# 真正决定音质上限的是频道自己的码率设置，在 KOOK 客户端里
# 「编辑频道 → 音质/码率」调，服务器管理员就能改，不用充钱。
# 这里填非 0 只是为了排查问题时强行覆盖，调高于频道上限没有用。
VOICE_BITRATE = int(os.getenv('VOICE_BITRATE', '0'))

# 低码率时降成单声道。32k 的立体声音乐会糊成一团，
# 同样的码率给单声道，每声道能分到的比特翻倍，人声和乐器会清楚不少。
# 填一个 kbps 阈值（比如 48）表示"码率低于它就转单声道"，0 = 永远立体声。
VOICE_MONO_BELOW = int(os.getenv('VOICE_MONO_BELOW', '0'))

# AI 随机插嘴概率
INTERJECT_CHANCE = 0.15


def _env_num(name: str, default, cast=int):
    """读数字型环境变量，没填或填错就用默认值。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return cast(raw.strip())
    except ValueError:
        print(f"⚠️ .env 里的 {name}={raw!r} 不是数字，先用默认值 {default}")
        return default


# ---------- 用量上限 ----------
# 默认值都给得很宽，朋友间的小服务器基本碰不到；
# 挂到人多的服务器上时，它们能挡住刷屏、爆内存和 AI 账单失控。
# 全部可以在 .env 里改，填 0 就是关掉这一项限制。
MAX_QUEUE = _env_num('MAX_QUEUE', 50)                      # 每个服务器最多排多少首歌
MAX_REMINDERS_PER_USER = _env_num('MAX_REMINDERS_PER_USER', 20)   # 每人最多挂多少条待触发的提醒
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

GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.6-flash')

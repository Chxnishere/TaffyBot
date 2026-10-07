"""歌曲解析和搜索。这一整个文件和聊天平台无关，从 Discord 版原样搬过来的，
只把写死的相对路径换成了 config 里的绝对路径。
"""
import html
import logging
import os
import asyncio
import re
import threading
import urllib.parse
from datetime import datetime

import aiohttp
import yt_dlp

from config import BROWSER_UA, COOKIE_PATH, PLAY_ALLOWED_HOSTS, SEARCH_LIMIT

logger = logging.getLogger(__name__)

# yt-dlp 每次解析完都会把 cookies.txt 整个重写一遍。
# 两个解析同时跑、或者一边写一边读，读到的就是半个文件（实测并发时 25% 的读取不完整）。
# 所以所有碰 cookies.txt 的地方都排这一把锁；用 RLock 是因为解析过程中还会回头读 cookie。
_ytdl_lock = threading.RLock()


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
    raise ValueError("/play 只收 YouTube / B站 的链接喵。想按歌名找请用 /search")

BILI_SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"

# 取流时用的基础请求头。注意这里**不要**写死 Referer：
# 以前的版本无论什么站都回落到 B 站的 Referer，结果把 B 站的 Referer
# 和整份 cookies.txt（含 B 站登录态）一起发给了 googlevideo，
# YouTube 于是返回一坨不是音频的东西，ffmpeg 报 Invalid data found。
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
    """按来源拼取流请求头。

    优先用 yt-dlp 自己给的 http_headers（它最清楚该带什么），
    缺失时才回落到 BASE_HEADERS，并且只补这个站该有的 Referer 和 cookie。
    """
    headers = dict(ytdl_headers or {})
    for k, v in BASE_HEADERS.items():
        headers.setdefault(k, v)

    rule = SOURCE_RULES.get(source, {})
    referer = rule.get('referer')
    if referer:
        headers.setdefault('Referer', referer)
    else:
        # 非 B 站就把 B 站的 Referer 清掉，别让它跟着跑到别家 CDN
        if 'bilibili.com' in (headers.get('Referer') or ''):
            headers.pop('Referer', None)

    cookie = ''
    for domain in rule.get('cookie_domains', []):
        c = get_cookies_for_domain(domain)
        if c:
            cookie = f'{cookie}; {c}' if cookie else c
    if cookie:
        headers['Cookie'] = cookie
    else:
        headers.pop('Cookie', None)
    return headers


def get_cookie_header() -> str:
    """整个 cookies.txt 拼成一条 Cookie 头，给 yt-dlp / 取流用。"""
    if not os.path.isfile(COOKIE_PATH):
        return ''
    try:
        pairs = []
        with open(COOKIE_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or not line:
                    continue
                parts = line.split('\t')
                if len(parts) >= 7:
                    pairs.append(f"{parts[5]}={parts[6]}")
        return '; '.join(pairs)
    except Exception as e:
        logger.error(f"读取 Cookie 失败: {e}")
        return ''


def get_cookies_for_domain(domain_suffix: str) -> str:
    """只取某个域名下的 cookie。

    和 get_cookie_header 分开写：那个是给取流用的，
    这个是给 taffy 自己调 API 用的，别把两边的 cookie 混着发出去。
    """
    if not os.path.isfile(COOKIE_PATH):
        return ''
    pairs = []
    try:
        # 和 yt-dlp 重写 cookies.txt 互斥。会等锁，所以协程里要用 asyncio.to_thread 调
        with _ytdl_lock, open(COOKIE_PATH, 'r', encoding='utf-8') as f:
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
        logger.error(f"读取 {domain_suffix} cookie 失败: {e}")
        return ''
    return '; '.join(pairs)


def pick_audio_format(info: dict):
    """yt-dlp 没直接给 url 时，自己从 formats 里挑一个音频流。

    返回 (直链, format_id, ext)，挑不出来就是 (None, None, None)。
    """
    formats = info.get('formats') or []
    usable = [f for f in formats
              if f.get('url') and f.get('acodec') not in (None, 'none')]
    if not usable:
        return None, None, None
    # 优先纯音频流，没有的话退而求其次用带视频的（ffmpeg 会只取音轨）
    audio_only = [f for f in usable if f.get('vcodec') in ('none', None)]
    pool = audio_only or usable
    best = max(pool, key=lambda f: f.get('abr') or f.get('tbr') or 0)
    return best.get('url'), best.get('format_id'), best.get('ext')


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
        # 正确的 key 是 http_headers；原来写的 'headers' yt-dlp 根本不认
        'http_headers': {'User-Agent': BROWSER_UA},
        'age_limit': 0,
    }
    if os.path.isfile(COOKIE_PATH):
        ydl_opts['cookiefile'] = COOKIE_PATH

    # 先拿锁再开 YoutubeDL：它退出时会重写 cookies.txt，要在锁里完成
    with _ytdl_lock, yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(query, download=False)
            if 'entries' in info:
                info = info['entries'][0]

            # 拿直链。**绝对不能**回落到 webpage_url：
            # 那是 YouTube 的播放页 HTML，喂给 ffmpeg 只会得到
            # "Invalid data found"，而且很难看出原因。
            direct_url = info.get('url')
            fmt_id, fmt_ext = info.get('format_id'), info.get('ext')
            if not direct_url:
                direct_url, fmt_id, fmt_ext = pick_audio_format(info)
            if not direct_url:
                page = info.get('webpage_url') or query
                raise ValueError(
                    "拿不到音频直链喵。常见原因：视频需要登录/年龄限制/地区封锁，"
                    "或者 yt-dlp 版本太旧跟不上网站改版。"
                    "先试 `pip install -U yt-dlp`，还不行就换一个视频。"
                    f"（{page}）"
                )

            extractor = (info.get('extractor') or '').lower()
            if 'youtube' in extractor or 'googlevideo.com' in direct_url:
                source = 'youtube'
            elif 'bilibili' in extractor or 'bilivideo' in direct_url:
                source = 'bilibili'
            else:
                source = 'unknown'

            http_headers = build_stream_headers(source, info.get('http_headers'))

            host = urllib.parse.urlsplit(direct_url).hostname or '?'
            logger.info(
                f"[{source}] 音频URL获取成功 -> host={host} "
                f"Referer={http_headers.get('Referer')} "
                f"含Cookie={'是' if http_headers.get('Cookie') else '否'} "
                f"format={fmt_id}/{fmt_ext}"
            )

            return {
                'title': info.get('title', '未知歌曲'),
                'url': direct_url,
                'webpage_url': info.get('webpage_url'),
                'resolved_at': datetime.now().timestamp(),
                'duration': info.get('duration', 0),
                'uploader': info.get('uploader', '未知'),
                'thumbnail': info.get('thumbnail'),
                'source': source,
                'http_headers': http_headers,
            }
        except Exception as e:
            raise ValueError(f"获取音频失败: {e}")


def format_duration(value) -> str:
    """B站给 "mm:ss" 字符串，YouTube 给秒数，统一成 mm:ss。"""
    if value is None or value == '':
        return '未知'
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r'\d{1,2}(:\d{1,2}){1,2}', value):
            return value
        return '未知'
    try:
        total = int(float(value))
    except (TypeError, ValueError):
        return '未知'
    if total <= 0:
        return '未知'
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def search_youtube(query: str, limit: int = SEARCH_LIMIT) -> list:
    """同步函数，调用方必须用 asyncio.to_thread 包起来。

    extract_flat 只拿搜索页元数据，不解析每个视频的直链，所以很快；
    也不带 sleep_interval，不然搜一次要等十几秒。
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
    if os.path.isfile(COOKIE_PATH):
        opts['cookiefile'] = COOKIE_PATH

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
            'title': entry.get('title') or '未知视频',
            'url': url,
            'uploader': entry.get('uploader') or entry.get('channel') or '未知',
            'duration': entry.get('duration'),
            'source': 'youtube',
        })
    return results[:limit]


async def search_bilibili(query: str, limit: int = SEARCH_LIMIT) -> list:
    """走 B 站自己的搜索 API。

    yt-dlp 的 bilisearch 扁平抽取拿不到标题，选单里只会显示一串 av 号。
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

    params = {'search_type': 'video', 'keyword': query, 'page': 1}
    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession() as session:
        async with session.get(BILI_SEARCH_URL, params=params,
                               headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                raise ValueError(f"B站搜索请求失败，状态码 {resp.status}")
            data = await resp.json(content_type=None)

    code = data.get('code')
    if code != 0:
        if code == -412:
            raise ValueError("B站搜索被风控了喵，可能要更新 cookies.txt 里的 B 站 cookie。")
        raise ValueError(f"B站搜索返回异常 (code={code}): {data.get('message', '未知错误')}")

    raw_results = ((data.get('data') or {}).get('result')) or []
    results = []
    for item in raw_results:
        if item.get('type') and item.get('type') != 'video':
            continue
        bvid = item.get('bvid')
        url = f"https://www.bilibili.com/video/{bvid}" if bvid else item.get('arcurl')
        if not url:
            continue
        # 搜索结果 title 带 <em class="keyword"> 高亮标签，要清掉
        title = html.unescape(re.sub(r'<[^>]+>', '', item.get('title') or '')).strip()
        results.append({
            'title': title or '未知视频',
            'url': url,
            'uploader': item.get('author') or '未知',
            'duration': item.get('duration'),
            'source': 'bilibili',
        })
        if len(results) >= limit:
            break
    return results

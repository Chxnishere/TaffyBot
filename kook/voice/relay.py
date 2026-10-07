"""本地取流中转。

为什么需要它
------------
Discord 版是这样放歌的：

    discord.FFmpegPCMAudio(url, before_options='-headers "Referer: ..."')

B 站的 CDN 直链不带 Referer 会直接 403，所以那个 `-headers` 是必需的。

而 kookvoice 里 ffmpeg 的命令行是写死的：

    f'{ffmpeg_bin} -nostats -i "{file}" -filter:a volume=0.4 ...'

`-i` 前面没有任何地方能塞自定义 header。这是整个移植里最硬的一堵墙。

解决办法
--------
在 127.0.0.1 上开一个极小的 HTTP 服务。塔菲把歌注册进来拿到一个
`http://127.0.0.1:17650/s/<token>`，把这个地址交给 kookvoice；
ffmpeg 来拉这个地址时，中转再带着正确的 Referer / UA / Cookie
去真正的 CDN 取流，然后把字节原样转回去。

顺带解决了直链过期的问题：Discord 版要在播放前手动
`_refresh_if_stale`，而这里是 ffmpeg 真正来取流的那一刻才去解析，
过期了就当场重新解析一次，时机永远是对的。
"""
import asyncio
import logging
from datetime import datetime

import aiohttp
from aiohttp import web

from config import RELAY_HOST, RELAY_PORT, URL_REFRESH_AFTER
from core.music_source import get_song_data

logger = logging.getLogger(__name__)

_registry = {}      # token -> song dict
_runner = None
_counter = 0

CHUNK = 64 * 1024


def register(song: dict) -> str:
    """把歌登记进来，返回给 ffmpeg 用的本地地址。"""
    global _counter
    _counter += 1
    token = f"{_counter}-{int(datetime.now().timestamp())}"
    _registry[token] = song
    return f"http://{RELAY_HOST}:{RELAY_PORT}/s/{token}"


def forget(token: str):
    _registry.pop(token, None)


async def _resolve_if_stale(song: dict) -> dict:
    """直链有有效期，排队太久就重新解析一次。

    这段逻辑和 Discord 版的 _refresh_if_stale 是一样的，
    只是调用时机从"播放前"挪到了"ffmpeg 真的来取流时"。
    """
    resolved_at = song.get('resolved_at', 0)
    if datetime.now().timestamp() - resolved_at < URL_REFRESH_AFTER:
        return song
    page_url = song.get('webpage_url')
    if not page_url:
        return song
    try:
        fresh = await asyncio.to_thread(get_song_data, page_url)
        logger.info(f"直链已过期，重新解析成功: {fresh.get('title')}")
        song.update(fresh)
    except Exception as e:
        logger.error(f"重新解析直链失败，沿用旧链接: {e}")
    return song


async def _handle(request: web.Request) -> web.StreamResponse:
    token = request.match_info['token']
    song = _registry.get(token)
    if song is None:
        raise web.HTTPNotFound(text='unknown token')

    song = await _resolve_if_stale(song)
    upstream = song.get('url')
    headers = dict(song.get('http_headers') or {})
    # 逐跳头不能转发
    headers.pop('Accept-Encoding', None)

    # ffmpeg 有时会发 Range，透传给上游
    if 'Range' in request.headers:
        headers['Range'] = request.headers['Range']

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)
    try:
        session = aiohttp.ClientSession(timeout=timeout)
        upstream_resp = await session.get(upstream, headers=headers)
    except Exception as e:
        logger.error(f"中转取流失败 {song.get('title')}: {e}")
        raise web.HTTPBadGateway(text=str(e))

    ctype = upstream_resp.headers.get('Content-Type', '')
    clen = upstream_resp.headers.get('Content-Length', '?')
    logger.info(f"取流 {song.get('title')} -> HTTP {upstream_resp.status} "
                f"{ctype} {clen}B")

    if upstream_resp.status >= 400:
        body = await upstream_resp.text()
        await session.close()
        logger.error(f"上游返回 {upstream_resp.status}: {body[:200]}")
        raise web.HTTPBadGateway(text=f'upstream {upstream_resp.status}')

    # 上游返回 200/206 但内容不是音视频，多半是被要求登录、被风控，
    # 或者请求头不对（比如把 B 站的 Referer/Cookie 发给了 googlevideo）。
    # 不在这里提前发现的话，ffmpeg 只会甩一句 "Invalid data found"。
    if ctype and not any(t in ctype for t in ('audio', 'video', 'octet-stream', 'mp4')):
        peek = (await upstream_resp.content.read(300)).decode(errors='replace')
        await session.close()
        logger.error(f"上游返回的不是音频（Content-Type={ctype}）。"
                     f"检查取流请求头是否发错了站。开头: {peek[:200]!r}")
        raise web.HTTPBadGateway(text='upstream not media')

    out_headers = {}
    for key in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
        if key in upstream_resp.headers:
            out_headers[key] = upstream_resp.headers[key]

    response = web.StreamResponse(status=upstream_resp.status, headers=out_headers)
    await response.prepare(request)

    try:
        async for chunk in upstream_resp.content.iter_chunked(CHUNK):
            await response.write(chunk)
        await response.write_eof()
    except (asyncio.CancelledError, ConnectionResetError):
        # ffmpeg 被 skip / stop 杀掉时是正常现象，不用报错
        pass
    except Exception as e:
        logger.error(f"中转推流中断 {song.get('title')}: {e}")
    finally:
        upstream_resp.release()
        await session.close()

    return response


async def start_relay():
    global _runner
    app = web.Application()
    app.router.add_get('/s/{token}', _handle)
    _runner = web.AppRunner(app)
    await _runner.setup()
    site = web.TCPSite(_runner, RELAY_HOST, RELAY_PORT)
    await site.start()
    logger.info(f"本地取流中转已启动: http://{RELAY_HOST}:{RELAY_PORT}")


async def stop_relay():
    if _runner is not None:
        await _runner.cleanup()

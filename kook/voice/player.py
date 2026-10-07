"""KOOK 语音播放。

为什么不用 kookvoice 的 Player
------------------------------
kookvoice 的推流是两个 ffmpeg 串起来的：
  p2 = ffmpeg 解码歌曲 -> PCM  （没有 -re，能多快解多快）
  p  = ffmpeg 读 PCM  -> RTP  （有 -re，按真实速度消费）

它的循环里写数据用的是 `p.stdin.write(...)`，**没有 await drain()**。
于是整首歌的 PCM 会在一瞬间全塞进 asyncio 的发送缓冲区，循环立刻跑完，
它就认为"放完了"，马上 `p.kill()` 并断开语音。
可 p 因为 -re 还在慢慢往外发，缓冲区里没发出去的部分全被丢掉。

源站慢的时候解码被网速拖住，这个 bug 会被掩盖。
而我们的本地取流中转一秒就能把整首歌喂完，于是必然触发：
机器人进语音 -> 推了不到一秒 -> 直接退出。

所以这里自己实现，但保留它"推流进程常驻"的正确部分：

  常驻 p_rtp : ffmpeg -re -f s16le -i pipe:0 -> RTP   （整个语音会话只起一次）
  每首 p_dec : ffmpeg -i <中转地址>        -> s16le PCM

两者之间由我们自己搬字节，并且**每写一块就 await drain()**。
drain 提供了背压：RTP 那边因为 -re 只按真实速度消费，
写入就会自然被卡住，绝不会跑到它前面去——这正是 kookvoice 缺的那一行。

推流进程常驻这件事不能省：RTP 的序列号和时间戳必须连续。
一首歌换一个 ffmpeg 的话，第二首会以同一个 SSRC 重新从随机序列号开始，
KOOK 的抖动缓冲会把它当成乱序包全部丢掉——现象就是第一首正常、
之后每一首都是静音。
"""
import asyncio
import logging
import os
import shlex
from datetime import datetime

import aiohttp

from config import FFMPEG_BIN, IDLE_TIMEOUT

# 音质相关的三个设置是后来才加进 config.py 的。
# 只更新了 player.py 没更新 config.py 的话，硬 import 会让整个 bot 起不来，
# 而这几个只是可选旋钮，有默认值就能跑——所以这里退回读环境变量，
# 同时明确提醒一句，不要让它悄悄变成"配置改了不生效"。
try:
    from config import VOICE_BITRATE, VOICE_MONO_BELOW, VOICE_VOLUME
except ImportError:
    VOICE_VOLUME = float(os.getenv('VOICE_VOLUME', '1.0'))
    VOICE_BITRATE = int(os.getenv('VOICE_BITRATE', '0'))
    VOICE_MONO_BELOW = int(os.getenv('VOICE_MONO_BELOW', '0'))
    logging.getLogger(__name__).warning(
        "config.py 里没有 VOICE_VOLUME / VOICE_BITRATE / VOICE_MONO_BELOW，"
        "先用默认值跑着。建议把这三行补进 config.py，否则 .env 里改了也可能不生效。")
from voice import relay

logger = logging.getLogger(__name__)

KOOK_API = 'https://www.kookapp.cn/api/v3'
KEEPALIVE_INTERVAL = 45
PCM_CHUNK = 48000 * 2 * 2 // 10   # 约 100ms 的 48kHz 立体声 PCM
# 队列放完后补多少个 100ms 的静音再退出，原因见 _flush_tail
TAIL_PAD_CHUNKS = 15

_players = {}


class VoiceAPI:
    """KOOK 语音频道的三个接口，直接打，不依赖 kookvoice。"""

    def __init__(self, token: str):
        self._headers = {'Authorization': f'Bot {token}'}

    async def _req(self, method: str, path: str, **kwargs):
        async with aiohttp.ClientSession(headers=self._headers) as s:
            async with s.request(method, f'{KOOK_API}/{path}', **kwargs) as r:
                data = await r.json(content_type=None)
        if data.get('code') != 0:
            raise RuntimeError(f"{path} 失败: {data.get('message')}")
        return data.get('data')

    async def join(self, channel_id):
        return await self._req('POST', 'voice/join', json={'channel_id': str(channel_id)})

    async def leave(self, channel_id):
        return await self._req('POST', 'voice/leave', json={'channel_id': str(channel_id)})

    async def keep_alive(self, channel_id):
        return await self._req('POST', 'voice/keep-alive',
                               json={'channel_id': str(channel_id)})


class GuildPlayer:
    """一个服务器一个。队列、当前曲目、推流进程都在这。"""

    def __init__(self, guild_id: str, token: str):
        self.guild_id = str(guild_id)
        self.api = VoiceAPI(token)
        self.voice_channel_id = None
        self.text_channel_id = None
        self.current = None
        self.queue = []
        self.idle_since = datetime.now().timestamp()
        self.stopped = False

        self._proc = None        # 当前这首歌的解码进程
        self._rtp = None         # 常驻推流进程，整个语音会话共用一个
        self._rtp_err = None
        self._runner = None
        self._keepalive = None
        self._skip = False
        self.on_track_start = None   # handlers/music.py 注入，用来发"正在播放"
        self.on_error = None         # handlers/music.py 注入，用来把播放失败告诉频道

    # ---------------- 队列 ----------------

    def enqueue(self, song: dict, voice_channel_id, text_channel_id):
        self.stopped = False
        self.voice_channel_id = str(voice_channel_id)
        self.text_channel_id = str(text_channel_id)

        # 交给 ffmpeg 的是本地中转地址，不是 CDN 直链，
        # 这样才能带上 B 站需要的 Referer。
        song = dict(song)
        song['_stream_url'] = relay.register(song)
        self.queue.append(song)
        self.idle_since = datetime.now().timestamp()

        if self._runner is None or self._runner.done():
            self._runner = asyncio.create_task(self._run())
        return song

    def skip(self) -> bool:
        if self._proc is None and not self.queue:
            return False
        self._skip = True
        self._kill_proc()
        return True

    def stop(self):
        self.stopped = True
        self._forget_all()       # 要在清队列之前，不然排队中的歌在中转里的登记清不掉
        self.queue.clear()
        self._kill_proc()
        self._kill_rtp()
        if self._runner and not self._runner.done():
            self._runner.cancel()
        self.current = None
        self.idle_since = datetime.now().timestamp()

    @property
    def is_playing(self) -> bool:
        return self.current is not None or bool(self.queue)

    def is_idle(self) -> bool:
        return (not self.is_playing
                and datetime.now().timestamp() - self.idle_since > IDLE_TIMEOUT)

    # ---------------- 内部 ----------------

    def _kill_proc(self):
        """只杀解码进程。推流进程要留着，否则 RTP 序列号会断。"""
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass

    def _kill_rtp(self):
        if self._rtp and self._rtp.returncode is None:
            try:
                self._rtp.stdin.close()
            except Exception:
                pass
            try:
                self._rtp.kill()
            except ProcessLookupError:
                pass
        if self._rtp_err:
            self._rtp_err.cancel()
        self._rtp = None

    def _forget_all(self):
        for song in self.queue + ([self.current] if self.current else []):
            url = song.get('_stream_url') or ''
            if url:
                relay.forget(url.rsplit('/', 1)[-1])

    async def _report(self, text: str):
        """把播放失败告诉文字频道。以前只写日志，用户那边就是"说了在放却没声音"。"""
        if self.on_error is None:
            return
        try:
            await self.on_error(self, text)
        except Exception as e:
            logger.error(f"发送播放失败提示失败: {e}")

    async def _flush_tail(self, channels: int):
        """队列放完后、退出语音前，往推流进程里补一小段静音。

        解码进程读完不等于听众听完：推流进程带 -re 按真实速度往外发，
        我们这边的写缓冲、管道、ffmpeg 自己的输入缓冲里还压着大约 1 秒的 PCM。
        以前解码一结束就直接 kill 推流进程，最后一首歌的结尾就被切掉了
        （实测 8 秒的队列只发出去 7.26 秒）。
        补静音并 drain：写得进去就说明前面真正的音乐已经被消费掉了，
        最后被切掉的只会是这段静音。
        不用"关 stdin 等它自己退出"的办法，是因为这期间可能有新歌进队列，
        推流进程必须留着（换进程 RTP 序列号会断，见文件头的说明）。
        """
        rtp = self._rtp
        if rtp is None or rtp.returncode is not None:
            return
        silence = bytes(48000 * 2 * channels // 10)   # 100ms
        try:
            for _ in range(TAIL_PAD_CHUNKS):
                if self.stopped or self.queue:
                    return   # 被 stop 了，或者来了新歌，不用补了
                rtp.stdin.write(silence)
                await rtp.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def _keepalive_loop(self, channel_id):
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                await self.api.keep_alive(channel_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"语音保活失败 guild={self.guild_id}: {e}")

    async def _run(self):
        """一首接一首地放，直到队列空或者被 stop。"""
        channel_id = self.voice_channel_id
        try:
            try:
                await self.api.leave(channel_id)   # 先退一次，避免上次没清干净
            except Exception:
                pass
            info = await self.api.join(channel_id)
        except Exception as e:
            logger.error(f"加入语音频道失败 guild={self.guild_id}: {e}")
            self._forget_all()
            self.queue.clear()
            await self._report(f"❌ 塔菲进不去语音频道喵（{e}），队列先清掉了。"
                               "检查一下咱在这个语音频道的权限。")
            return

        rtp_url = f"rtp://{info['ip']}:{info['port']}?rtcpport={info['rtcp_port']}"
        # KOOK 给这个频道分配的码率就是音质上限。
        # 原来这里还照抄了 kookvoice 的 *0.9，白白又砍一成，没有理由。
        bitrate = VOICE_BITRATE or int(info.get('bitrate', 96000) / 1000)

        channels = 1 if (VOICE_MONO_BELOW and bitrate < VOICE_MONO_BELOW) else 2
        self._keepalive = asyncio.create_task(self._keepalive_loop(channel_id))
        logger.info(f"已加入语音频道 {channel_id}，RTP -> "
                    f"{info['ip']}:{info['port']} @{bitrate}k "
                    f"{'单声道' if channels == 1 else '立体声'}")

        cancelled = False
        try:
            while not self.stopped:
                while self.queue and not self.stopped:
                    song = self.queue.pop(0)
                    self.current = song
                    self.idle_since = datetime.now().timestamp()
                    self._skip = False
                    if self.on_track_start:
                        try:
                            await self.on_track_start(self, song)
                        except Exception as e:
                            logger.error(f"播放开始回调失败: {e}")
                    await self._play_one(song, rtp_url, bitrate, channels)
                    relay.forget(song.get('_stream_url', '').rsplit('/', 1)[-1])
                    self.current = None
                if self.stopped:
                    break
                # 队列空了：先把还压在缓冲里的结尾放完，再决定走不走。
                # 这期间如果有人又点了歌，就回到上面接着放。
                await self._flush_tail(channels)
                if not self.queue:
                    break
        except asyncio.CancelledError:
            cancelled = True
        finally:
            self.current = None
            self._kill_rtp()
            if self._keepalive:
                self._keepalive.cancel()
            try:
                await self.api.leave(channel_id)
            except Exception as e:
                logger.debug(f"退出语音频道失败: {e}")
            self.idle_since = datetime.now().timestamp()
            logger.info(f"已退出语音频道 {channel_id}")
            # 正在退出语音的这一小会儿里如果有人点了歌，enqueue 看到 runner 还没结束
            # 就不会另起一个，那首歌会一直躺在队列里。这里补起一个。
            if self.queue and not self.stopped and not cancelled:
                self._runner = asyncio.create_task(self._run())

    # ---------------- 推流进程（常驻） ----------------

    def rtp_cmd(self, rtp_url: str, bitrate: int, channels: int) -> list:
        """常驻的推流进程：从 stdin 收 PCM，编码成 opus 推 RTP。

        -re 让它按真实速度消费 stdin，这就是整条链路的节拍来源。
        """
        return [
            FFMPEG_BIN, '-re',
            '-f', 's16le', '-ar', '48000', '-ac', str(channels), '-i', 'pipe:0',
            '-nostats', '-loglevel', 'error',
            # -map 不能省：原始 PCM 输入时 tee 不会自动选流，
            # 会直接报 "Output file does not contain any stream"
            '-map', '0:a:0',
            '-acodec', 'libopus',
            '-b:a', f'{bitrate}k',
            # application=audio 是给音乐用的（voip 档会为了人声牺牲高频）
            '-application', 'audio',
            # constrained 而不是 on：RTP 这边码率固定，
            # 不受约束的 VBR 冲高会被信道丢包，反而更难听
            '-vbr', 'constrained',
            '-compression_level', '10',
            '-frame_duration', '20',
            '-ac', str(channels), '-ar', '48000',
            '-f', 'tee',
            f'[select=a:f=rtp:ssrc=1111:payload_type=111]{rtp_url}',
        ]

    def decode_cmd(self, stream_url: str, channels: int) -> list:
        """每首歌一个：把音频解成 48kHz s16le PCM 写到 stdout。

        这里不加 -re：节奏由推流进程的 -re 加上 drain() 的背压来控制。
        """
        # 源基本都是 44.1kHz，KOOK 要 48kHz，必然要重采样。
        # ffmpeg 默认重采样器质量一般，soxr 明显更干净，几乎不吃 CPU。
        filters = ['aresample=resampler=soxr:precision=28:osr=48000']
        if abs(VOICE_VOLUME - 1.0) > 1e-6:
            filters.append(f'volume={VOICE_VOLUME}')
        return [
            FFMPEG_BIN,
            '-reconnect', '1', '-reconnect_streamed', '1',
            '-reconnect_delay_max', '5',
            '-nostats', '-loglevel', 'error',
            '-i', stream_url,
            '-vn', '-map', '0:a:0',
            '-filter:a', ','.join(filters),
            '-f', 's16le', '-ar', '48000', '-ac', str(channels),
            'pipe:1',
        ]

    async def _ensure_rtp(self, rtp_url: str, bitrate: int, channels: int):
        if self._rtp is not None and self._rtp.returncode is None:
            return self._rtp
        cmd = self.rtp_cmd(rtp_url, bitrate, channels)
        logger.debug("rtp ffmpeg: " + " ".join(shlex.quote(c) for c in cmd))
        self._rtp = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._rtp_err = asyncio.create_task(self._watch_rtp(self._rtp))
        return self._rtp

    async def _watch_rtp(self, proc):
        """推流进程的 stderr 一定要看着，kookvoice 把它扔进了 /dev/null。"""
        try:
            err = await proc.stderr.read()
            rc = await proc.wait()
            if rc not in (0, None) and not self.stopped:
                logger.error(f"推流进程退出 rc={rc}: "
                             f"{err.decode(errors='replace')[:400]}")
        except asyncio.CancelledError:
            pass

    async def _play_one(self, song: dict, rtp_url: str, bitrate: int, channels: int):
        """解码一首歌，把 PCM 搬进常驻推流进程。"""
        rtp = await self._ensure_rtp(rtp_url, bitrate, channels)
        cmd = self.decode_cmd(song['_stream_url'], channels)
        logger.debug("decode ffmpeg: " + " ".join(shlex.quote(c) for c in cmd))
        try:
            dec = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error(f"找不到 ffmpeg（FFMPEG_BIN={FFMPEG_BIN}）")
            return
        self._proc = dec

        try:
            while True:
                chunk = await dec.stdout.read(PCM_CHUNK)
                if not chunk:
                    break
                if rtp.returncode is not None:
                    logger.error("推流进程已经死了，这首歌放不下去了")
                    break
                rtp.stdin.write(chunk)
                # 关键的一行：等推流进程真的收下了再继续。
                # 没有它就会像 kookvoice 那样，把整首歌塞进缓冲区
                # 然后以为放完了。
                await rtp.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"写入推流进程失败: {e}")
        except asyncio.CancelledError:
            raise
        finally:
            err = b''
            if dec.returncode is None:
                try:
                    dec.kill()
                except ProcessLookupError:
                    pass
            try:
                err = await dec.stderr.read()
            except Exception:
                pass
            await dec.wait()
            self._proc = None

        if self._skip:
            logger.info(f"已跳过: {song.get('title')}")
            return
        if dec.returncode not in (0, None) and not self.stopped:
            logger.error(f"解码异常 rc={dec.returncode} 歌曲={song.get('title')}: "
                         f"{err.decode(errors='replace')[:400]}")
            await self._report(f"❌ 播放「{song.get('title', '未知')}」时出错了喵，"
                               "这首先跳过。换个链接或者过会儿再试试。")


def get_player(guild_id, token: str) -> GuildPlayer:
    guild_id = str(guild_id)
    if guild_id not in _players:
        _players[guild_id] = GuildPlayer(guild_id, token)
    return _players[guild_id]


def peek_player(guild_id):
    return _players.get(str(guild_id))


def drop_player(guild_id):
    p = _players.pop(str(guild_id), None)
    if p:
        p.stop()


def all_players():
    return list(_players.items())

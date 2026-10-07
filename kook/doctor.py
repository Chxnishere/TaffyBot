#!/usr/bin/env python3
"""启动前自检。不连 KOOK 也能跑大半，专门用来回答"为什么它不动"。

    python3 doctor.py

每一项要么 ✅ 要么给出具体该改哪里，不会只说"失败了"。
"""
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
ok_count = 0
fail_count = 0
warn_count = 0


def ok(msg):
    global ok_count
    ok_count += 1
    print(f"  ✅ {msg}")


def warn(msg, hint=''):
    global warn_count
    warn_count += 1
    print(f"  ⚠️  {msg}")
    if hint:
        print(f"      → {hint}")


def bad(msg, hint=''):
    global fail_count
    fail_count += 1
    print(f"  ❌ {msg}")
    if hint:
        print(f"      → {hint}")


def section(name):
    print(f"\n── {name} " + "─" * max(0, 50 - len(name)))


def check_python():
    section("Python")
    v = sys.version_info
    if v >= (3, 9):
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        bad(f"Python {v.major}.{v.minor}", "需要 3.9 以上")
    if (BASE / '.venv').exists():
        running_in_venv = str(BASE / '.venv') in sys.executable
        if running_in_venv:
            ok(f"正在使用 venv: {sys.executable}")
        else:
            warn(f"有 .venv 但当前用的是 {sys.executable}",
                 "source .venv/bin/activate 之后再跑，"
                 "systemd 的 ExecStart 也要指向 .venv/bin/python3")


def check_deps():
    section("依赖")
    for mod, why in [('khl', 'KOOK SDK，缺了什么都跑不了'),
                     ('aiohttp', 'HTTP 和取流中转'),
                     ('yt_dlp', '歌曲解析'),
                     ('dotenv', '读 .env'),
                     ('google.genai', 'AI 聊天')]:
        try:
            __import__(mod)
            ok(f"{mod}")
        except ImportError:
            bad(f"{mod} 没装（{why}）", "pip install -r requirements.txt")


def check_logging():
    section("日志")
    import logging
    if logging.root.manager.disable >= logging.CRITICAL:
        bad("日志被全局关掉了（logging.disable 生效中）",
            "有库调了 logging.disable()，它是全进程总闸，"
            "会把健康报告也一起静音")
    else:
        ok("日志没有被全局禁用")


def check_ffmpeg():
    section("ffmpeg")
    path = shutil.which(os.getenv('FFMPEG_BIN', 'ffmpeg'))
    if not path:
        warn("找不到 ffmpeg", "sudo apt install -y ffmpeg。只影响音乐功能")
        return
    try:
        out = subprocess.run([path, '-version'], capture_output=True,
                             timeout=10).stdout.decode().splitlines()[0]
        ok(f"{path} — {out[:60]}")
    except Exception as e:
        warn(f"ffmpeg 跑不起来: {e}")


def check_env():
    section(".env")
    env_path = BASE / '.env'
    if not env_path.exists():
        bad(".env 不存在", f"在项目目录新建一个 .env，把 KOOK_TOKEN 等填进去，路径应为 {env_path}")
        return False
    ok(f".env 存在 ({env_path})")

    from dotenv import load_dotenv
    load_dotenv(env_path)

    raw = os.getenv('KOOK_TOKEN') or ''
    token = raw.strip()
    if not token:
        bad("KOOK_TOKEN 是空的", "去 developer.kookapp.cn 复制机器人 token")
        return False
    if raw != token:
        warn("KOOK_TOKEN 前后有空格或换行", "代码里已经 strip 了，但最好还是清掉")
    # 不打印 token 的任何一段：这份输出经常被整段贴出去求助
    ok(f"KOOK_TOKEN 已填（{len(token)} 字符）")

    for key, what in [('GEMINI_API_KEY', 'AI 聊天'),
                      ('OWM_API_KEY', '/sky 和每日天气'),
                      ('GENERAL_ID', '每日天气播报频道'),
                      ('LOOT_ID', '每日折扣播报频道')]:
        if os.getenv(key):
            ok(f"{key} 已填（{what}）")
        else:
            warn(f"{key} 没填 → {what} 不可用")
    return True


def check_files():
    section("文件")
    skills = BASE / 'skills'
    found = [f for f in ('persona.md', 'distillation.md', 'self-reference.md')
             if (skills / f).exists()]
    if len(found) == 3:
        ok("三个人设文件齐了")
    elif found:
        warn(f"人设文件只有 {found}", f"把缺的放进 {skills}/")
    else:
        warn("skills/ 里没有人设文件", f"塔菲会变成普通助手。放到 {skills}/")

    cookie = Path(os.getenv('COOKIE_PATH') or (BASE / 'cookies.txt'))
    if cookie.exists():
        ok(f"cookies.txt 存在（{cookie.stat().st_size} 字节）")
    else:
        warn("cookies.txt 不存在", "只影响音乐，/play 和 /search 大概率会失败")

    db = Path(os.getenv('DB_PATH') or (BASE / 'taffybot.db'))
    if db.exists():
        ok(f"数据库已存在 {db}")
    else:
        ok(f"数据库会新建在 {db}")
    if not os.access(db.parent, os.W_OK):
        bad(f"{db.parent} 不可写", "检查目录属主，systemd 里 User= 要和目录属主一致")


def check_lock():
    section("单实例锁")
    import fcntl
    lock_path = os.getenv('LOCK_PATH') or '/tmp/taffybot-kook.lock'
    try:
        fp = open(lock_path, 'w')
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fp, fcntl.LOCK_UN)
        ok(f"锁空闲 {lock_path}")
    except BlockingIOError:
        bad(f"锁被占用 {lock_path}",
            "已经有一个实例在跑。sudo systemctl stop taffy-kook，"
            "或者 ps aux | grep main.py")
    except Exception as e:
        warn(f"锁检查失败: {e}")


async def check_net():
    section("网络")
    import aiohttp
    targets = [
        ('KOOK API', 'https://www.kookapp.cn/api/v3/gateway', True),
        ('YouTube', 'https://www.youtube.com', False),
        ('Bilibili API', 'https://api.bilibili.com', False),
        ('Gemini', 'https://generativelanguage.googleapis.com', False),
    ]
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        for name, url, critical in targets:
            try:
                async with s.get(url) as r:
                    ok(f"{name} 可达 (HTTP {r.status})")
            except Exception as e:
                if critical:
                    bad(f"{name} 不可达: {type(e).__name__}",
                        "这个区域连不上 KOOK，换新加坡/东京的实例")
                else:
                    warn(f"{name} 不可达: {type(e).__name__}",
                         "相关功能会失败，其他不受影响")


async def check_channels():
    section("播报频道")
    token = (os.getenv('KOOK_TOKEN') or '').strip()
    targets = [('GENERAL_ID', '每日天气 10:00'), ('LOOT_ID', '每日折扣 22:00')]
    if not token:
        warn("没有 token，跳过频道校验")
        return

    import aiohttp
    for key, what in targets:
        cid = (os.getenv(key) or '').strip()
        if not cid:
            warn(f"{key} 没填 → {what} 不会播报")
            continue

        # Discord 的雪花 id 是 17-19 位，KOOK 的明显更短。
        if cid.isdigit() and len(cid) >= 17:
            bad(f"{key}={cid} 看起来是 Discord 的频道 id（{len(cid)} 位）",
                "KOOK 的频道 id 要在 KOOK 客户端里取："
                "设置里打开开发者模式，右键频道 → 复制 ID")
            continue

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as s2:
                async with s2.get('https://www.kookapp.cn/api/v3/channel/view',
                                  params={'target_id': cid},
                                  headers={'Authorization': f'Bot {token}'}) as r:
                    text = await r.text()
            import json as _json
            data = _json.loads(text)
        except ValueError:
            bad(f"{key} 校验时 KOOK 返回的不是 JSON",
                f"网络被拦截或走了代理。返回开头: {text[:80]!r}")
            continue
        except Exception as e:
            warn(f"{key} 校验失败: {type(e).__name__}: {e}")
            continue

        if data.get('code') == 0:
            d = data.get('data') or {}
            kind = '文字' if d.get('type') == 1 else '语音' if d.get('type') == 2 else '?'
            if d.get('type') != 1:
                bad(f"{key} 指向的是{kind}频道「{d.get('name')}」", "播报要发到文字频道")
            else:
                ok(f"{key} -> 「{d.get('name')}」（{what}）")
        else:
            bad(f"{key}={cid} 这个频道打不开: {data.get('message')}",
                "id 填错了，或者机器人不在这个服务器/没有该频道权限")


async def check_token():
    section("token 有效性")
    token = (os.getenv('KOOK_TOKEN') or '').strip()
    if not token:
        warn("没有 token，跳过")
        return
    import aiohttp
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get('https://www.kookapp.cn/api/v3/user/me',
                             headers={'Authorization': f'Bot {token}'}) as r:
                text = await r.text()
        try:
            import json as _json
            data = _json.loads(text)
        except ValueError:
            # 返回的不是 JSON，通常是被代理/防火墙拦了，或者根本没连到 KOOK。
            # khl.py 遇到这种情况会抛那个看不懂的 "must be a mapping, not bytes"。
            bad(f"KOOK 返回的不是 JSON（HTTP {r.status}）",
                f"多半是网络被拦截或走了代理。返回内容开头: {text[:80]!r}")
            return
        if isinstance(data, dict) and data.get('code') == 0:
            me = data.get('data', {})
            ok(f"token 有效 — 机器人是 {me.get('username')} (ID: {me.get('id')})")
        else:
            bad(f"token 被拒绝: {data}",
                "重新去 developer.kookapp.cn 复制，注意别带空格")
    except Exception as e:
        bad(f"验证 token 时出错: {type(e).__name__}: {e}")


def check_relay_port():
    section("取流中转端口")
    import socket
    port = int(os.getenv('RELAY_PORT', '17650'))
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', port))
        ok(f"127.0.0.1:{port} 可用")
    except OSError as e:
        bad(f"127.0.0.1:{port} 绑不上: {e}",
            "换个 RELAY_PORT，或看看谁占了：sudo lsof -i :%d" % port)
    finally:
        s.close()


def check_cards():
    section("卡片限制自查")
    try:
        import core.deals as D
        D.stores_cache = {'1': 'Steam'}
        from render import cards
        from handlers.common import check_card
        samples = {
            '天气卡': cards.weather_card('东京', 'JP', '晴', 22, 21, 55, 3.2),
            '搜索卡(5结果)': cards.search_card('s', 'YouTube', 'q', [
                {'title': f't{i}', 'uploader': 'u', 'duration': 60} for i in range(5)]),
            '帮助卡': cards.help_card(),
            '投票卡': cards.poll_card({'id': 'p', 'question': 'q', 'options': ['a', 'b'],
                                    'votes': {}, 'closed': False}),
        }
        deals = [{'title': f'G{i}', 'storeID': '1', 'normalPrice': '10',
                  'salePrice': '1', 'savings': '90', 'dealID': str(i)} for i in range(10)]
        for i, cm in enumerate(cards.deal_cards(deals), 1):
            samples[f'折扣卡第{i}条'] = cm
        for name, cm in samples.items():
            probs = check_card(cm)
            if probs:
                bad(f"{name}: " + "; ".join(probs))
            else:
                ok(name)
    except Exception as e:
        bad(f"卡片自查跑不起来: {type(e).__name__}: {e}")


def check_rtp_command():
    section("推流命令")
    import subprocess
    ffmpeg = shutil.which(os.getenv('FFMPEG_BIN', 'ffmpeg'))
    if not ffmpeg:
        warn("没有 ffmpeg，跳过")
        return
    try:
        enc = subprocess.run([ffmpeg, '-hide_banner', '-encoders'],
                             capture_output=True, timeout=15).stdout.decode()
        muxers = subprocess.run([ffmpeg, '-hide_banner', '-muxers'],
                                capture_output=True, timeout=15).stdout.decode()
        if 'libopus' in enc:
            ok("libopus 编码器可用")
        else:
            bad("ffmpeg 没有 libopus 编码器", "KOOK 语音必须用 opus，换一个带 libopus 的 ffmpeg 构建")
        for m in ('tee', 'rtp'):
            if f' {m} ' in muxers:
                ok(f"{m} 封装器可用")
            else:
                bad(f"ffmpeg 没有 {m} 封装器")
    except Exception as e:
        warn(f"推流命令自检失败: {e}")


def check_audio_settings():
    section("音质设置")
    try:
        from config import VOICE_BITRATE, VOICE_MONO_BELOW, VOICE_VOLUME
        if abs(VOICE_VOLUME - 1.0) < 1e-6:
            ok("音量倍率 1.0（原样输出）")
        elif VOICE_VOLUME < 0.6:
            warn(f"音量倍率 {VOICE_VOLUME}，偏低",
                 "听着闷/糊多半就是这里。1.0 才是原始音量")
        else:
            ok(f"音量倍率 {VOICE_VOLUME}")
        ok(f"码率：{'跟随频道设置' if not VOICE_BITRATE else str(VOICE_BITRATE) + 'k（强制）'}")
        ok(f"低码率转单声道：{'关闭' if not VOICE_MONO_BELOW else '低于 ' + str(VOICE_MONO_BELOW) + 'k 时启用'}")

        import subprocess
        ff = shutil.which(os.getenv('FFMPEG_BIN', 'ffmpeg'))
        if ff:
            ver = subprocess.run([ff, '-version'], capture_output=True,
                                 timeout=10).stdout.decode()
            if 'enable-libsoxr' in ver:
                ok("libsoxr 可用（44.1k→48k 重采样更干净）")
            else:
                warn("ffmpeg 没带 libsoxr",
                     "重采样会退回默认算法，音质略差但能用")
        print("      → 音质上限由频道码率决定：KOOK 客户端「编辑频道 → 音质/码率」，"
              "管理员即可调整")
    except Exception as e:
        warn(f"音质设置自查失败: {e}")


def check_url_parsing():
    section("链接解析")
    try:
        from handlers.common import clean_query
        cases = [
            ('[https://youtu.be/abc?si=x](https://youtu.be/abc?si=x)', 'https://youtu.be/abc?si=x'),
            ('[看这个](https://www.bilibili.com/video/BV1xx)', 'https://www.bilibili.com/video/BV1xx'),
            ('<https://youtu.be/abc>', 'https://youtu.be/abc'),
            ('https://youtu.be/abc', 'https://youtu.be/abc'),
            ('告白气球', '告白气球'),
        ]
        for raw, expect in cases:
            got = clean_query(raw)
            if got == expect:
                ok(f"{raw[:38]:40} -> {got[:40]}")
            else:
                bad(f"{raw[:38]} 解析成了 {got}，应该是 {expect}")
    except Exception as e:
        bad(f"链接解析自查失败: {type(e).__name__}: {e}")


def check_extractor():
    section("yt-dlp / 取流解析")
    try:
        import yt_dlp
        ver = getattr(yt_dlp.version, '__version__', '?')
        ok(f"yt-dlp {ver}")
        warn_hint = ("YouTube 改版很频繁，拿不到直链时第一件事就是 "
                     "`pip install -U yt-dlp`")
        print(f"      → {warn_hint}")
    except Exception as e:
        bad(f"yt-dlp 读不出版本: {e}")
        return

    url = None
    for i, a in enumerate(sys.argv):
        if a in ('--url', '-u') and i + 1 < len(sys.argv):
            url = sys.argv[i + 1]
    if not url:
        print("      → 想测某个具体视频：python3 doctor.py --url <链接>")
        return

    try:
        from core.music_source import get_song_data
        from handlers.common import clean_query
        song = get_song_data(clean_query(url))
        import urllib.parse
        host = urllib.parse.urlsplit(song['url']).hostname
        ok(f"解析成功: {song['title'][:40]}")
        ok(f"  来源={song['source']} host={host}")
        ok(f"  Referer={song['http_headers'].get('Referer')} "
           f"含Cookie={'是' if song['http_headers'].get('Cookie') else '否'}")
        if host and ('youtube.com' in host or 'bilibili.com' in host):
            bad("直链指向的是网页而不是媒体服务器",
                "说明没选到真正的音频流，多半要更新 yt-dlp")
    except Exception as e:
        bad(f"解析失败: {e}")


async def main():
    print("塔菲 KOOK 版 · 启动前自检")
    print(f"项目目录: {BASE}")
    check_python()
    check_deps()
    check_logging()
    check_ffmpeg()
    has_env = check_env()
    check_files()
    check_lock()
    check_relay_port()
    check_cards()
    check_url_parsing()
    check_rtp_command()
    check_extractor()
    check_audio_settings()
    await check_net()
    if has_env:
        await check_token()
        await check_channels()

    print("\n" + "=" * 54)
    print(f"  ✅ {ok_count} 项通过   ⚠️  {warn_count} 项警告   ❌ {fail_count} 项失败")
    if fail_count:
        print("  先把 ❌ 修掉再启动喵。")
    elif warn_count:
        print("  可以启动了，⚠️ 的那些功能会缺席。")
    else:
        print("  全绿，python3 main.py 走起喵！")
    print("=" * 54)
    return 1 if fail_count else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))

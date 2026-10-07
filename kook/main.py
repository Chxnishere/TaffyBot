"""塔菲 KOOK 版入口。

和 Discord 版 on_ready 的差别：
- 指令同步那一整块删掉了。KOOK 没有 application command，
  指令在定义时就注册好了，不需要 sync，也不需要 GUILD_ID。
- Cog 加载换成各 handler 模块的 setup(bot)，而且要在 bot.run() 之前跑完，
  khl.py 是定义即注册，不是连上之后再注册。
- 健康报告留着，它是确认移植有没有落地的最快方式。
"""
import asyncio
import fcntl
import logging
import os
import sys

from khl import Bot, EventTypes

import config
from core import ai
from core.db import init_db, pending_reminder_count
from handlers import chat, deals, misc, music, party, poll, remind, weather
from handlers.common import dispatch_button
from voice import relay

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)
# 移植期间想看连线细节就把下面这行改成 DEBUG
logging.getLogger('khl').setLevel(logging.WARNING)
logging.getLogger('apscheduler').setLevel(logging.WARNING)

# 单实例锁。同一个 token 跑两个进程会重复回复。
# 路径带上平台名，方便 Discord 版和 KOOK 版并行跑。
_lock_fp = open(config.LOCK_PATH, 'w')
try:
    fcntl.flock(_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("⚠️ 检测到已有一个塔菲实例在运行，本进程退出喵！")
    sys.exit(1)

if not config.TOKEN:
    print("❌ .env 里没有 KOOK_TOKEN，起不来喵！")
    sys.exit(1)

bot = Bot(token=config.TOKEN)

_me = {'id': None, 'username': None}


def bot_user_id():
    return _me['id']


# ---------------- 注册 ----------------
misc.setup(bot)
weather.setup(bot)
deals.setup(bot)
remind.setup(bot)
poll.setup(bot)
party.setup(bot)
music.setup(bot)
chat.setup(bot)

import tasks  # noqa: E402  放在 handlers 之后，它依赖 handlers.common
tasks.setup(bot, bot_user_id)


@bot.on_event(EventTypes.MESSAGE_BTN_CLICK)
async def on_button_click(b, event):
    """KOOK 所有按钮点击都走这一个入口，按 value 前缀自己分发。"""
    await dispatch_button(b, event)


@bot.on_startup
async def startup(b: Bot):
    try:
        me = await b.client.fetch_me()
    except Exception as e:
        # token 不对时 khl.py 会抛一个很难看的
        # "User() argument after ** must be a mapping, not bytes"，
        # 因为 KOOK 返回的不是 JSON。翻译成人话。
        logger.error("=" * 50)
        logger.error("❌ 连接 KOOK 失败，拿不到机器人自己的信息。")
        logger.error("   最常见的原因是 KOOK_TOKEN 不对或粘贴时多了空格/换行。")
        logger.error("   请检查 .env 里的 KOOK_TOKEN，以及机器人连接模式是否为 WebSocket。")
        logger.error(f"   原始错误: {type(e).__name__}: {e}")
        logger.error("=" * 50)
        raise SystemExit(1)

    _me['id'] = me.id
    _me['username'] = me.username
    chat.set_bot_identity(me.id)

    await relay.start_relay()

    await party.restore_parties(b)
    await poll.restore_polls(b)

    async def _channel_label(cid, what):
        """启动时就把频道 id 解析成名字。
        显示成「频道名」比一串 id 有用得多——id 填错了一眼就能看出来。"""
        if not cid:
            return '❌ 未配置播报频道'
        try:
            ch = await b.client.fetch_public_channel(str(cid))
            return f'✅ 「{ch.name}」'
        except Exception as e:
            return (f'❌ {cid} 打不开（{e}）。'
                    f'KOOK 频道 id 约 16 位，17-19 位多半是 Discord 的 id')

    loot_label = await _channel_label(config.LOOT_ID, '折扣')
    general_label = await _channel_label(config.GENERAL_ID, '天气')
    ai_ok = ai.is_ready()
    report = [
        "=== 塔菲 Bot（KOOK）启动健康报告 ===",
        f"🤖 机器人：{me.username} (ID: {me.id})",
        f"🧠 AI 功能：{'✅ 已启用' if ai_ok else '❌ 未配置'}",
        f"🎵 音乐播放：{'✅ Cookie 有效' if os.path.isfile(config.COOKIE_PATH) else '❌ Cookie 缺失'}",
        f"🔊 取流中转：✅ http://{config.RELAY_HOST}:{config.RELAY_PORT}",
        f"☁️  天气功能：{'✅ 已配置' if config.OWM_KEY else '❌ 未配置'}",
        f"💰 折扣播报：{loot_label}",
        f"🌤️  天气播报：{general_label}",
        f"💾 数据库：✅ {config.DB_PATH}",
        f"⏰ 待触发提醒：{pending_reminder_count()} 条",
        "===================================",
    ]
    for line in report:
        logger.info(line)


@bot.on_shutdown
async def shutdown(b: Bot):
    await relay.stop_relay()


if __name__ == '__main__':
    try:
        init_db()
        logger.info(f"数据库已就绪喵: {config.DB_PATH}")
    except Exception as e:
        logger.error(f"❌ 数据库初始化失败，无法启动: {e}")
        sys.exit(1)

    if not os.path.isfile(config.COOKIE_PATH):
        logger.warning(f"⚠️ Cookie 文件不存在 ({config.COOKIE_PATH})，音乐播放可能不可用！")

    ai.init_ai(platform="KOOK")
    bot.run()

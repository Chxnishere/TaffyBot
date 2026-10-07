"""/poll 和它的按钮。

Discord 的 PollView 把票数存在 view 对象里，重启就没了。
KOOK 这边票数落库，卡片上的按钮重启后照样能点。
"""
import asyncio
import logging
import shlex
import uuid
from datetime import datetime

from khl import Bot, Message

from core.db import load_open_polls, load_poll, save_poll
from handlers.common import (button_channel_id, button_user_id, on_button,
                             send_card, send_text, update_card)
from render import cards

logger = logging.getLogger(__name__)

POLL_TIMEOUT = 60


async def _close(bot, poll: dict):
    if poll['closed']:
        return
    poll['closed'] = True
    save_poll(poll)
    if poll.get('msg_id'):
        try:
            await update_card(bot, poll['msg_id'], cards.poll_card(poll))
        except Exception as e:
            logger.debug(f"关闭投票卡片失败: {e}")
    try:
        await send_text(bot, poll['channel_id'], "📊 **投票结束！**")
    except Exception:
        pass


async def _delayed_close(bot, poll_id: str, delay: float):
    try:
        await asyncio.sleep(delay)
        poll = load_poll(poll_id)
        if poll and not poll['closed']:
            await _close(bot, poll)
    except asyncio.CancelledError:
        pass


@on_button('poll')
async def _poll_button(bot, event, parts):
    if len(parts) < 4 or parts[1] != 'vote':
        return
    poll = load_poll(parts[2])
    channel_id = button_channel_id(event)
    user_id = button_user_id(event)

    if poll is None or poll['closed']:
        await send_text(bot, channel_id, "这个投票已经结束了喵！", temp_target_id=user_id)
        return

    try:
        index = int(parts[3])
    except ValueError:
        return
    if not 0 <= index < len(poll['options']):
        return

    if poll['votes'].get(user_id) == index:
        await send_text(bot, channel_id, "你已经投给这个选项了喵！", temp_target_id=user_id)
        return

    poll['votes'][user_id] = index
    save_poll(poll)
    if poll.get('msg_id'):
        await update_card(bot, poll['msg_id'], cards.poll_card(poll))
    await send_text(bot, channel_id, "投票/改票成功喵！", temp_target_id=user_id)


async def restore_polls(bot):
    now = datetime.now().timestamp()
    restored = 0
    for poll in load_open_polls():
        remaining = poll['expire_ts'] - now
        if remaining <= 0:
            await _close(bot, poll)
        else:
            asyncio.create_task(_delayed_close(bot, poll['id'], remaining))
            restored += 1
    if restored:
        logger.info(f"恢复了 {restored} 个进行中的投票喵！")


def setup(bot: Bot):

    @bot.command(name='poll')
    async def poll_cmd(msg: Message, *args):
        """KOOK 按空格切词：带空格的问题/选项请用引号包起来，
        这里用 shlex 重新解析一次原始内容。
        """
        raw = msg.content.strip()
        # 去掉命令本身
        for prefix in ('/poll', '/Poll'):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = list(args)

        if len(tokens) < 3:
            await msg.reply(
                "用法：`/poll <问题> <选项1> <选项2>`\n"
                "带空格的内容用引号包起来，例如：\n"
                "`/poll \"今晚打什么\" 原神 星穹铁道`",
                is_temp=True)
            return

        question, option1, option2 = tokens[0], tokens[1], tokens[2]
        poll = {
            'id': uuid.uuid4().hex[:12],
            'channel_id': msg.target_id,
            'msg_id': None,
            'question': question,
            'options': [option1, option2],
            'votes': {},
            'closed': False,
            'expire_ts': datetime.now().timestamp() + POLL_TIMEOUT,
        }
        resp = await send_card(bot, poll['channel_id'], cards.poll_card(poll))
        poll['msg_id'] = (resp or {}).get('msg_id')
        save_poll(poll)
        asyncio.create_task(_delayed_close(bot, poll['id'], POLL_TIMEOUT))

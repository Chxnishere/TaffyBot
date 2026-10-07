"""/remind /reminders /cancelreminder

/remind 的第一个 token 是时间，剩下全是内容。
"""
import logging
from datetime import datetime

from khl import Bot, Message

from config import CST, MAX_REMINDERS_PER_USER
from core.db import (add_reminder, cancel_reminder, list_reminders,
                     parse_duration, user_reminder_count)
from handlers.common import rest
from render import cards

logger = logging.getLogger(__name__)


def setup(bot: Bot):

    @bot.command(name='remind')
    async def remind(msg: Message, when: str = '', *what_args):
        seconds = parse_duration(when)
        if seconds is None:
            await msg.reply(
                "时间看不懂喵！写成 `30m`、`2h`、`1d`、`1小时30分` 这种，最长一年。",
                is_temp=True)
            return

        what = rest(what_args)
        if not what:
            await msg.reply("要提醒什么呀喵？", is_temp=True)
            return
        what = what[:500]

        due_ts = datetime.now().timestamp() + seconds
        guild_id = getattr(msg.ctx.guild, 'id', None) if msg.ctx.guild else None
        try:
            if MAX_REMINDERS_PER_USER > 0:
                pending = user_reminder_count(msg.author_id)
                if pending >= MAX_REMINDERS_PER_USER:
                    await msg.reply(
                        f"你已经有 {pending} 条提醒在排队了喵，"
                        "先用 `/reminders` 看看、`/cancelreminder` 取消几条吧~", is_temp=True)
                    return
            reminder_id = add_reminder(msg.author_id, msg.target_id, guild_id, due_ts, what)
        except Exception as e:
            logger.error(f"写入提醒失败: {e}")
            await msg.reply("提醒没存上喵，稍后再试试~", is_temp=True)
            return

        due_local = datetime.fromtimestamp(due_ts, CST).strftime('%Y-%m-%d %H:%M')
        await msg.reply(
            f"⏰ 记下了喵！`#{reminder_id}` 会在 **{due_local}**（UTC+8）提醒你：**{what}**")

    @bot.command(name='reminders')
    async def reminders_list(msg: Message, *args):
        try:
            rows = list_reminders(msg.author_id)
        except Exception as e:
            logger.error(f"读取提醒列表失败: {e}")
            await msg.reply("读不出来喵，稍后再试试~", is_temp=True)
            return

        if not rows:
            await msg.reply("你现在没有待办的提醒喵~", is_temp=True)
            return
        await msg.reply(cards.reminders_card(rows), is_temp=True)

    @bot.command(name='cancelreminder')
    async def cancel_reminder_cmd(msg: Message, reminder_id: str = '', *args):
        if not reminder_id.isdigit():
            await msg.reply("要取消哪条呀喵？用法：`/cancelreminder 3`，编号用 `/reminders` 查。",
                            is_temp=True)
            return
        try:
            ok = cancel_reminder(int(reminder_id), msg.author_id)
        except Exception as e:
            logger.error(f"取消提醒失败: {e}")
            await msg.reply("取消失败喵，稍后再试试~", is_temp=True)
            return

        if ok:
            await msg.reply(f"🗑️ 提醒 `#{reminder_id}` 取消了喵！", is_temp=True)
        else:
            await msg.reply(
                "没找到这条提醒喵，可能已经触发了、已经取消了，或者不是你设的。",
                is_temp=True)

"""/party 和它的按钮。

KOOK 只给一个全局的 message_btn_click 事件，带上按钮里写死的 value。
所以 value 写成 `party:toggle:<party_id>`，在 common.dispatch_button 里按前缀路由。

状态落在 SQLite，重启后组队卡片的按钮照样能点。
"""
import asyncio
import logging
from datetime import datetime

from khl import Bot, Message

from core.db import load_open_parties, load_party, save_party
from core.party import PARTY_TIMEOUT, PartySession
from handlers.common import (button_channel_id, button_msg_id, button_user_id,
                             dm, on_button, send_card, send_text, update_card)
from render import cards

logger = logging.getLogger(__name__)

_sessions = {}   # party_id -> PartySession（内存里的活跃副本，锁在这上面）
_bot = None


def _get_session(party_id: str):
    """内存里没有就从数据库捞（比如刚重启）。"""
    if party_id in _sessions:
        return _sessions[party_id]
    d = load_party(party_id)
    if d is None:
        return None
    session = PartySession.from_dict(d)
    _sessions[party_id] = session
    return session


async def _leader_info(bot, leader_id: str):
    try:
        user = await bot.client.fetch_user(str(leader_id))
        return user.nickname or user.username, user.avatar
    except Exception:
        return None, None


async def _render(bot, session: PartySession):
    name, avatar = await _leader_info(bot, session.leader_id)
    return cards.party_card(session, name, avatar)


async def _refresh(bot, session: PartySession):
    if not session.msg_id:
        return
    try:
        await update_card(bot, session.msg_id, await _render(bot, session))
    except Exception as e:
        logger.debug(f"刷新组队卡片失败 {session.id}: {e}")


async def _notify(bot, user_id: str, session: PartySession, action: str):
    await dm(bot, user_id, f"你已{action}队伍「{session.game_name}」喵！")
    if str(user_id) == session.leader_id:
        return
    await dm(bot, session.leader_id,
             f"有人{action}了队伍「{session.game_name}」喵！当前 {len(session.members)} 人。")


async def _close(bot, session: PartySession, reason: str):
    if session.closed:
        return
    session.closed = True
    if session.timeout_task:
        session.timeout_task.cancel()
    save_party(session.to_dict())
    await _refresh(bot, session)

    if reason == "满员":
        text = f"(met)all(met) 🎉 组队成功！「{session.game_name}」队伍已满员！"
    elif reason == "超时":
        text = f"(met)all(met) ⏰ 组队结束！「{session.game_name}」队伍招募超时。"
    else:
        text = f"(met)all(met) 🚫 组队解散！「{session.game_name}」队伍已解散。"

    try:
        await send_text(bot, session.channel_id, text)
    except Exception as e:
        logger.error(f"发送组队结束通知失败: {e}")
    _sessions.pop(session.id, None)


async def _delayed_close(bot, session: PartySession, delay: float):
    try:
        await asyncio.sleep(delay)
        if not session.closed:
            # 这个字符串必须和 _close 里的判断完全一致，否则会走到"解散"分支
            await _close(bot, session, "超时")
    except asyncio.CancelledError:
        pass


@on_button('party')
async def _party_button(bot, event, parts):
    if len(parts) < 3 or parts[1] != 'toggle':
        return
    session = _get_session(parts[2])
    channel_id = button_channel_id(event)
    user_id = button_user_id(event)

    if session is None or session.closed:
        await send_text(bot, channel_id, "队伍已关闭喵！", temp_target_id=user_id)
        return

    async with session.lock:
        # 成员判断必须在锁里做，否则并发点按会重复加入/退出
        if session.closed:
            await send_text(bot, channel_id, "队伍已关闭喵！", temp_target_id=user_id)
            return

        if user_id in session.members:
            if user_id == session.leader_id:
                session.members.remove(user_id)
                await _close(bot, session, "解散")
                await send_text(bot, channel_id, "你已退出队伍，队伍已解散喵！",
                                temp_target_id=user_id)
                return
            session.members.remove(user_id)
            save_party(session.to_dict())
            await _refresh(bot, session)
            await send_text(bot, channel_id, "你已退出队伍喵！", temp_target_id=user_id)
            await _notify(bot, user_id, session, "退出")
            return

        if session.is_full():
            await send_text(bot, channel_id, "队伍已满喵！", temp_target_id=user_id)
            return

        session.members.append(user_id)
        save_party(session.to_dict())
        if session.is_full():
            await _close(bot, session, "满员")
            await send_text(bot, channel_id, "你已加入队伍，队伍已满员！",
                            temp_target_id=user_id)
        else:
            await _refresh(bot, session)
            await send_text(bot, channel_id, "你已加入队伍喵！", temp_target_id=user_id)
        await _notify(bot, user_id, session, "加入")


async def restore_parties(bot):
    """重启后把还没关的组队捡回来，剩余时间继续跑。"""
    now = datetime.now().timestamp()
    for d in load_open_parties():
        session = PartySession.from_dict(d)
        _sessions[session.id] = session
        remaining = session.expire_ts - now
        if remaining <= 0:
            await _close(bot, session, "超时")
        else:
            session.timeout_task = asyncio.create_task(_delayed_close(bot, session, remaining))
    if _sessions:
        logger.info(f"恢复了 {len(_sessions)} 个进行中的组队喵！")


def setup(bot: Bot):
    global _bot
    _bot = bot

    @bot.command(name='party')
    async def party(msg: Message, *args):
        """KOOK 按空格切词，约定：最后一个 token 如果是纯数字就当人数上限。"""
        if not args:
            await msg.reply("要开什么游戏的房呀喵？用法：`/party 元神 4`", is_temp=True)
            return

        args = list(args)
        max_members = 0
        if len(args) > 1 and args[-1].isdigit():
            max_members = int(args[-1])
            args = args[:-1]
        game = " ".join(args).strip()

        if max_members < 0:
            await msg.reply("人数不能为负数喵！", is_temp=True)
            return
        if not game:
            await msg.reply("游戏名不能是空的喵！", is_temp=True)
            return

        session = PartySession(
            game_name=game,
            max_members=max_members,
            leader_id=msg.author_id,
            channel_id=msg.target_id,
            guild_id=getattr(msg.ctx.guild, 'id', None) if msg.ctx.guild else None,
        )
        _sessions[session.id] = session

        resp = await send_card(bot, session.channel_id, await _render(bot, session))
        session.msg_id = (resp or {}).get('msg_id')
        save_party(session.to_dict())

        session.timeout_task = asyncio.create_task(
            _delayed_close(bot, session, PARTY_TIMEOUT))

        cap = max_members if max_members > 0 else '不限'
        await dm(bot, msg.author_id, f"组队「{game}」已创建，招募上限 {cap} 人喵！")

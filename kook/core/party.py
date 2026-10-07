"""组队房间的状态机。

Discord 版里 PartySession 同时抱着 message 和 view 两个 Discord 对象，
这里把它们摘掉了：这个文件只管"谁在队里、满没满、关没关"，
渲染和消息 id 交给 handlers/party.py。
"""
import asyncio
import uuid
from datetime import datetime

PARTY_TIMEOUT = 60  # 秒，和 Discord 版保持一致


class PartySession:
    def __init__(self, game_name: str, max_members: int, leader_id: str,
                 channel_id: str, guild_id=None, party_id: str = None,
                 members=None, closed: bool = False,
                 created_ts: float = None, expire_ts: float = None,
                 msg_id: str = None):
        self.lock = asyncio.Lock()
        self.id = party_id or uuid.uuid4().hex[:12]
        self.game_name = game_name
        self.max_members = max_members
        self.leader_id = str(leader_id)
        self.channel_id = str(channel_id)
        self.guild_id = str(guild_id) if guild_id else None
        self.members = list(members) if members is not None else [self.leader_id]
        self.closed = closed
        self.msg_id = msg_id
        self.created_ts = created_ts or datetime.now().timestamp()
        self.expire_ts = expire_ts or (self.created_ts + PARTY_TIMEOUT)
        self.timeout_task = None

    def is_full(self) -> bool:
        return self.max_members != 0 and len(self.members) >= self.max_members

    def is_open(self) -> bool:
        return self.max_members == 0

    def status_text(self) -> str:
        if self.closed:
            return "已关闭"
        if self.is_full():
            return "已满员"
        return "招募中"

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'guild_id': self.guild_id,
            'channel_id': self.channel_id,
            'msg_id': self.msg_id,
            'leader_id': self.leader_id,
            'game_name': self.game_name,
            'max_members': self.max_members,
            'members': self.members,
            'closed': self.closed,
            'created_ts': self.created_ts,
            'expire_ts': self.expire_ts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'PartySession':
        return cls(
            game_name=d['game_name'],
            max_members=d['max_members'],
            leader_id=d['leader_id'],
            channel_id=d['channel_id'],
            guild_id=d.get('guild_id'),
            party_id=d['id'],
            members=d['members'],
            closed=d['closed'],
            created_ts=d['created_ts'],
            expire_ts=d['expire_ts'],
            msg_id=d.get('msg_id'),
        )

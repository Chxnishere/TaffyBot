"""SQLite 持久化层。

1. 所有 id 列都是 TEXT。KOOK 的 id 是字符串，塞进 INTEGER 列不会报错，
   但 `WHERE user_id=?` 拿字符串去比对整数在 SQLite 里匹配不上，
   会变成"提醒能触发但查不出也取消不掉"这种很难查的故障。
2. 访问数据库都过一把 threading.Lock。sqlite3 用了 check_same_thread=False，
   而这个项目里 yt-dlp 跑在 asyncio.to_thread 里，将来有人在线程里写库就会撞车。
3. parties / polls 两张表让组队和投票是持久的：KOOK 的按钮天然带 value，
   重启后按钮还能用。
"""
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime

from config import DB_PATH, DEAL_HISTORY_DAYS, MAX_REMINDER_SECONDS

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS posted_deals(
    deal_id   TEXT PRIMARY KEY,
    title     TEXT,
    posted_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deals_posted ON posted_deals(posted_ts);

CREATE TABLE IF NOT EXISTS reminders(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    guild_id   TEXT,
    created_ts REAL NOT NULL,
    due_ts     REAL NOT NULL,
    text       TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(done, due_ts);
CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders(user_id, done);

CREATE TABLE IF NOT EXISTS kv(
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS parties(
    id         TEXT PRIMARY KEY,
    guild_id   TEXT,
    channel_id TEXT NOT NULL,
    msg_id     TEXT,
    leader_id  TEXT NOT NULL,
    game_name  TEXT NOT NULL,
    max_members INTEGER NOT NULL,
    members    TEXT NOT NULL,
    closed     INTEGER NOT NULL DEFAULT 0,
    created_ts REAL NOT NULL,
    expire_ts  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS polls(
    id         TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    msg_id     TEXT,
    question   TEXT NOT NULL,
    options    TEXT NOT NULL,
    votes      TEXT NOT NULL,
    closed     INTEGER NOT NULL DEFAULT 0,
    expire_ts  REAL NOT NULL
);
"""

db = None
_lock = threading.Lock()


def init_db():
    """启动时调一次。WAL 让读写不互相阻塞。"""
    global db
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    db.commit()
    return db


def _exec(sql, params=()):
    with _lock:
        cur = db.execute(sql, params)
        db.commit()
        return cur


def _query(sql, params=()):
    with _lock:
        return db.execute(sql, params).fetchall()


def _query_one(sql, params=()):
    rows = _query(sql, params)
    return rows[0] if rows else None


# ---------- 通用 key-value ----------

def kv_set(key: str, value):
    _exec(
        "INSERT INTO kv(key, value, updated_ts) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
        (key, json.dumps(value, ensure_ascii=False), datetime.now().timestamp())
    )


def kv_get(key: str, max_age_seconds: float = None):
    row = _query_one("SELECT value, updated_ts FROM kv WHERE key=?", (key,))
    if row is None:
        return None
    if max_age_seconds is not None:
        if datetime.now().timestamp() - row['updated_ts'] > max_age_seconds:
            return None
    try:
        return json.loads(row['value'])
    except json.JSONDecodeError:
        return None


# ---------- 折扣去重 ----------

def filter_unposted_deals(deals: list) -> list:
    """挑出还没播报过的折扣，避免同一个特价连播好几天。"""
    if not deals:
        return []
    ids = [d.get('dealID') for d in deals if d.get('dealID')]
    if not ids:
        return deals
    seen = set()
    for i in range(0, len(ids), 500):  # SQLite 变量上限 999
        chunk = ids[i:i + 500]
        placeholders = ','.join('?' * len(chunk))
        rows = _query(
            f"SELECT deal_id FROM posted_deals WHERE deal_id IN ({placeholders})", chunk
        )
        seen.update(r['deal_id'] for r in rows)
    return [d for d in deals if d.get('dealID') and d['dealID'] not in seen]


def mark_deals_posted(deals: list):
    now = datetime.now().timestamp()
    rows = [(d['dealID'], d.get('title', ''), now) for d in deals if d.get('dealID')]
    if not rows:
        return
    with _lock:
        db.executemany(
            "INSERT OR REPLACE INTO posted_deals(deal_id, title, posted_ts) VALUES(?,?,?)", rows
        )
        db.commit()


def prune_old_deals(days: int = DEAL_HISTORY_DAYS) -> int:
    cutoff = datetime.now().timestamp() - days * 86400
    return _exec("DELETE FROM posted_deals WHERE posted_ts < ?", (cutoff,)).rowcount


# ---------- 提醒 ----------

DURATION_PATTERN = re.compile(
    r'(\d+)\s*(小时|分钟|星期|秒|分|时|天|日|周|[smhdw])', re.IGNORECASE
)
DURATION_UNITS = {
    's': 1, '秒': 1,
    'm': 60, '分': 60, '分钟': 60,
    'h': 3600, '时': 3600, '小时': 3600,
    'd': 86400, '天': 86400, '日': 86400,
    'w': 604800, '周': 604800, '星期': 604800,
}


def parse_duration(text: str):
    """把 '2h' / '1小时30分' / '3d' 解析成秒数，解析不出来返回 None。"""
    if not text:
        return None
    matches = DURATION_PATTERN.findall(text)
    if not matches:
        return None
    leftover = DURATION_PATTERN.sub('', text).strip()
    if leftover:
        return None
    total = 0
    for amount, unit in matches:
        mult = DURATION_UNITS.get(unit.lower())
        if mult is None:
            return None
        total += int(amount) * mult
    if total <= 0 or total > MAX_REMINDER_SECONDS:
        return None
    return total


def format_remaining(seconds: float) -> str:
    """剩余秒数写成人话。format_duration 是给歌曲时长用的，一周会被写成 168:00:00。"""
    seconds = int(seconds)
    if seconds <= 0:
        return "马上"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes and not days:
        parts.append(f"{minutes}分钟")
    if not parts:
        return "不到1分钟"
    return ''.join(parts)


def add_reminder(user_id: str, channel_id: str, guild_id, due_ts: float, text: str) -> int:
    cur = _exec(
        "INSERT INTO reminders(user_id, channel_id, guild_id, created_ts, due_ts, text) "
        "VALUES(?,?,?,?,?,?)",
        (str(user_id), str(channel_id), str(guild_id) if guild_id else None,
         datetime.now().timestamp(), due_ts, text)
    )
    return cur.lastrowid


def due_reminders(limit: int = 20) -> list:
    return _query(
        "SELECT * FROM reminders WHERE done=0 AND due_ts<=? ORDER BY due_ts LIMIT ?",
        (datetime.now().timestamp(), limit)
    )


def complete_reminder(reminder_id: int):
    _exec("UPDATE reminders SET done=1 WHERE id=?", (reminder_id,))


def list_reminders(user_id: str, limit: int = 15) -> list:
    return _query(
        "SELECT * FROM reminders WHERE user_id=? AND done=0 ORDER BY due_ts LIMIT ?",
        (str(user_id), limit)
    )


def cancel_reminder(reminder_id: int, user_id: str) -> bool:
    """只能取消自己的提醒。"""
    return _exec(
        "UPDATE reminders SET done=1 WHERE id=? AND user_id=? AND done=0",
        (reminder_id, str(user_id))
    ).rowcount > 0


def user_reminder_count(user_id: str) -> int:
    row = _query_one("SELECT COUNT(*) AS c FROM reminders WHERE user_id=? AND done=0",
                     (str(user_id),))
    return row['c'] if row else 0


def pending_reminder_count() -> int:
    row = _query_one("SELECT COUNT(*) AS c FROM reminders WHERE done=0")
    return row['c'] if row else 0


# ---------- 组队 ----------

def save_party(p: dict):
    _exec(
        "INSERT INTO parties(id, guild_id, channel_id, msg_id, leader_id, game_name, "
        "max_members, members, closed, created_ts, expire_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET msg_id=excluded.msg_id, members=excluded.members, "
        "closed=excluded.closed",
        (p['id'], p.get('guild_id'), p['channel_id'], p.get('msg_id'), p['leader_id'],
         p['game_name'], p['max_members'], json.dumps(p['members']),
         1 if p['closed'] else 0, p['created_ts'], p['expire_ts'])
    )


def load_party(party_id: str):
    row = _query_one("SELECT * FROM parties WHERE id=?", (party_id,))
    if not row:
        return None
    d = dict(row)
    d['members'] = json.loads(d['members'])
    d['closed'] = bool(d['closed'])
    return d


def load_open_parties() -> list:
    rows = _query("SELECT * FROM parties WHERE closed=0")
    out = []
    for row in rows:
        d = dict(row)
        d['members'] = json.loads(d['members'])
        d['closed'] = False
        out.append(d)
    return out


# ---------- 投票 ----------

def save_poll(p: dict):
    _exec(
        "INSERT INTO polls(id, channel_id, msg_id, question, options, votes, closed, expire_ts) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET msg_id=excluded.msg_id, votes=excluded.votes, "
        "closed=excluded.closed",
        (p['id'], p['channel_id'], p.get('msg_id'), p['question'],
         json.dumps(p['options'], ensure_ascii=False), json.dumps(p['votes']),
         1 if p['closed'] else 0, p['expire_ts'])
    )


def load_poll(poll_id: str):
    row = _query_one("SELECT * FROM polls WHERE id=?", (poll_id,))
    if not row:
        return None
    d = dict(row)
    d['options'] = json.loads(d['options'])
    d['votes'] = json.loads(d['votes'])
    d['closed'] = bool(d['closed'])
    return d


def load_open_polls() -> list:
    rows = _query("SELECT * FROM polls WHERE closed=0")
    out = []
    for row in rows:
        d = dict(row)
        d['options'] = json.loads(d['options'])
        d['votes'] = json.loads(d['votes'])
        d['closed'] = False
        out.append(d)
    return out

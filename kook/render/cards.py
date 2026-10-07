"""所有卡片构造。Discord 版的 discord.Embed 全部在这里变成 KOOK CardMessage。

对应关系（移植时的速查表）：
  embed.title                -> Module.Header（纯文本，不吃 markdown）
  embed.description          -> Module.Section(Element.Text(kmarkdown))
  add_field(inline=False)    -> 再来一个 Module.Section
  add_field(inline=True) x3  -> kv() 渲染成每行 `**标签**：值`
                                （不要用 Struct.Paragraph，cols 在移动端会被忽略）
  set_thumbnail              -> Section 的 accessory=Element.Image + mode=right
  set_footer                 -> Module.Context
  color=discord.Color.x()    -> Card(theme=Types.Theme.X)

注意 KOOK 的卡片不会渲染外站图片，除非先上传到 KOOK 的资源服务器。
头像和 KOOK 自己的图是本来就在 KOOK 上的，可以直接引用；
外部 CDN 的图（B站/YouTube 封面、OWM 图标）要走 assets.upload_external。
"""
import json
from datetime import datetime

from khl.card import Card, CardMessage, Element, Module, Types

from config import CST
from core.deals import as_float, get_store_name
from core.music_source import format_duration

# KOOK 官方限制（developer.kookapp.cn/doc/cardmessage）
MAX_CARDS_PER_MESSAGE = 5      # 一条消息最多 5 个卡片
MAX_MODULES_PER_MESSAGE = 50   # 一条消息所有卡片的模块数之和最多 50
MAX_BUTTONS_PER_GROUP = 4      # 一个 action-group 最多 4 个按钮
MAX_HEADER_CHARS = 100


def kv(*pairs) -> str:
    """把 (标签, 值) 渲染成每行一条的 `**标签**：值`。

    本来这里用的是 Struct.Paragraph(cols=3)，但官方文档写得很清楚：
    paragraph 的 cols "移动端忽略该参数"。
    于是手机上三栏会摊平成一串——先是三个标签，再是三个值，
    完全对不上号。kmarkdown 每行一条在所有端上都一样。
    """
    return "\n".join(f"**{k}**：{v}" for k, v in pairs)


# 状态 -> 主题色，对应 Discord 版的 green / gold / red
STATUS_THEME = {
    "招募中": Types.Theme.SUCCESS,
    "已满员": Types.Theme.WARNING,
    "已关闭": Types.Theme.DANGER,
}


def _mention(user_id: str) -> str:
    """KOOK 的 @ 语法。Discord 是 <@id>，这里是 (met)id(met)。"""
    return f"(met){user_id}(met)"


def simple(text: str, theme=Types.Theme.PRIMARY) -> CardMessage:
    """一句话卡片，给不需要结构的提示用。"""
    return CardMessage(Card(Module.Section(Element.Text(text, Types.Text.KMD)), theme=theme))


# ---------------- 折扣 ----------------

def deal_card(deal: dict, index: int = None) -> Card:
    title = deal.get('title', '未知游戏')
    store_name = get_store_name(deal.get('storeID', ''))
    normal_price = deal.get('normalPrice', 'N/A')
    sale_price = deal.get('salePrice', 'N/A')
    savings = deal.get('savings', '0')
    try:
        savings_str = f"{float(savings):.2f}%"
    except (TypeError, ValueError):
        savings_str = f"{savings}%"

    deal_id = deal.get('dealID', '')
    link = f"https://www.cheapshark.com/redirect?dealID={deal_id}" if deal_id else ""

    card = Card(theme=Types.Theme.SUCCESS)
    card.append(Module.Header(title[:100]))
    # Discord 版把链接挂在 embed.url 上，KOOK 没有这个位置，写进正文
    desc = f"🛒 **{store_name}**"
    if link:
        desc += f"  ·  [前往购买]({link})"
    card.append(Module.Section(Element.Text(desc, Types.Text.KMD)))
    card.append(Module.Section(Element.Text(kv(
        ("原价", f"${normal_price}"),
        ("现价", f"${sale_price}"),
        ("折扣", savings_str),
    ), Types.Text.KMD)))
    if index is not None:
        card.append(Module.Context(Element.Text(f"#{index + 1}", Types.Text.KMD)))
    return card


def count_modules(card: Card) -> int:
    return len(json.loads(json.dumps(CardMessage(card), ensure_ascii=False))[0]['modules'])


def chunk_cards(card_list: list) -> list:
    """把一堆卡片切成若干条合法的 CardMessage。

    KOOK 的两条硬限制（超了就是 40000 卡片消息json没有通过验证）：
      - 一条消息最多 5 个卡片
      - 一条消息里所有卡片的模块数之和最多 50
    /hotdeals 要发 10 个卡片，所以必须切开发两条。
    """
    messages, cur, cur_modules = [], [], 0
    for card in card_list:
        n = count_modules(card)
        if cur and (len(cur) >= MAX_CARDS_PER_MESSAGE
                    or cur_modules + n > MAX_MODULES_PER_MESSAGE):
            messages.append(CardMessage(*cur))
            cur, cur_modules = [], 0
        cur.append(card)
        cur_modules += n
    if cur:
        messages.append(CardMessage(*cur))
    return messages


def deal_cards(deals: list) -> list:
    """返回一个 CardMessage 列表，调用方要逐条发。"""
    return chunk_cards([deal_card(d, i) for i, d in enumerate(deals)])


def deal_broadcast_text(deals: list, limit_chars: int) -> tuple:
    """每日播报用纯文本，不用卡片：一次 25 条堆卡片太长。

    返回 (文本, 实际放进去的条数)。
    Discord 版这里有个 bug：按长度截断了但把整批都标记成已播报，
    被截掉的那些以后永远不会再播。所以这里把真实条数也返回出去。
    """
    header = "(met)all(met) 🎮 **今日优质游戏折扣速报！**"
    tail = "...（更多请使用 /hotdeals 查看喵！）"
    lines = [header]
    total = len(header) + 1
    posted = 0

    for idx, d in enumerate(deals):
        store_name = get_store_name(d.get('storeID', ''))
        savings = d.get('savings', '0')
        try:
            savings_str = f"{float(savings):.2f}%"
        except (TypeError, ValueError):
            savings_str = f"{savings}%"
        line = (
            f"{idx + 1}. **{d.get('title', '未知')}** - {store_name} | "
            f"~~${d.get('normalPrice', '?')}~~ → **${d.get('salePrice', '?')}** ({savings_str} off)"
        )
        if total + len(line) + 1 + len(tail) > limit_chars:
            lines.append(tail)
            break
        lines.append(line)
        total += len(line) + 1
        posted += 1

    return "\n".join(lines), posted


# ---------------- 天气 ----------------

def weather_card(zh_name, country_code, desc, temp, feels_like, humidity, wind_speed) -> CardMessage:
    card = Card(theme=Types.Theme.INFO)
    card.append(Module.Header(f"{zh_name} 即时天气播报喵"))
    card.append(Module.Section(Element.Text(f"当前天气状况：**{desc}**", Types.Text.KMD)))
    card.append(Module.Section(Element.Text(kv(
        ("🌍 国家/地区", country_code or "未知"),
        ("🌡️ 当前温度", f"{temp}°C"),
        ("🤔 体感温度", f"{feels_like}°C"),
        ("💧 相对湿度", f"{humidity}%"),
        ("💨 风速", f"{wind_speed} m/s"),
    ), Types.Text.KMD)))
    card.append(Module.Context(Element.Text("Powered by OpenWeatherMap & TaffyBot", Types.Text.KMD)))
    return CardMessage(card)


# ---------------- 组队 ----------------

def party_card(session, leader_name: str = None, leader_avatar: str = None) -> CardMessage:
    status = session.status_text()
    card = Card(theme=STATUS_THEME.get(status, Types.Theme.PRIMARY))
    card.append(Module.Header(f"🎮 组队：{session.game_name}"))

    cap = session.max_members if session.max_members > 0 else '∞'
    body = kv(
        ("👑 队长", _mention(session.leader_id)),
        ("👥 人数", f"{len(session.members)}/{cap}"),
        ("📌 状态", status),
    )
    if leader_avatar:
        card.append(Module.Section(
            Element.Text(body, Types.Text.KMD),
            accessory=Element.Image(leader_avatar, size=Types.Size.SM),
            mode=Types.SectionMode.RIGHT,
        ))
    else:
        card.append(Module.Section(Element.Text(body, Types.Text.KMD)))

    card.append(Module.Divider())
    members = "\n".join(_mention(uid) for uid in session.members) or "暂无成员"
    card.append(Module.Section(Element.Text(f"**📋 成员名单**\n{members}", Types.Text.KMD)))

    if not session.closed:
        card.append(Module.ActionGroup(Element.Button(
            "🔁 加入/退出",
            f"party:toggle:{session.id}",
            Types.Click.RETURN_VAL,
            Types.Theme.PRIMARY,
        )))
    return CardMessage(card)


# ---------------- 投票 ----------------

def poll_card(poll: dict) -> CardMessage:
    counts = [0] * len(poll['options'])
    for opt_index in poll['votes'].values():
        if 0 <= opt_index < len(counts):
            counts[opt_index] += 1

    theme = Types.Theme.SECONDARY if poll['closed'] else Types.Theme.PRIMARY
    card = Card(theme=theme)
    card.append(Module.Header(f"📊 投票：{poll['question']}"[:100]))

    if poll['closed']:
        card.append(Module.Section(Element.Text("🔒 **此投票已结束喵！**", Types.Text.KMD)))
    else:
        card.append(Module.Section(Element.Text("点击下方按钮为你支持的选项投票喵！", Types.Text.KMD)))

    tally = "\n".join(
        f"- **{opt}** — {counts[i]} 票" for i, opt in enumerate(poll['options'])
    )
    card.append(Module.Section(Element.Text(tally, Types.Text.KMD)))

    if not poll['closed']:
        # KOOK 的按钮没有 disabled，只能在结束时整个不再渲染 ActionGroup
        vote_buttons = [
            Element.Button(
                f"{opt} ({counts[i]})",
                f"poll:vote:{poll['id']}:{i}",
                Types.Click.RETURN_VAL,
                Types.Theme.PRIMARY,
            )
            for i, opt in enumerate(poll['options'])
        ]
        for i in range(0, len(vote_buttons), MAX_BUTTONS_PER_GROUP):
            card.append(Module.ActionGroup(*vote_buttons[i:i + MAX_BUTTONS_PER_GROUP]))
    return CardMessage(card)


# ---------------- 搜索 ----------------

def search_card(search_id: str, platform_label: str, query: str, results: list) -> CardMessage:
    """Discord 版是下拉选单，KOOK 卡片没有 select，改成一排编号按钮。

    正文把 5 条结果完整列出来，其实比原来的下拉更好读：
    下拉的 label/description 都被截到 95 字。
    """
    card = Card(theme=Types.Theme.PRIMARY)
    card.append(Module.Header(f"🔍 {platform_label} 搜索：{query}"[:100]))
    card.append(Module.Section(Element.Text("60 秒内点下面的编号，塔菲就去放喵～", Types.Text.KMD)))
    card.append(Module.Divider())

    lines = []
    for i, item in enumerate(results, 1):
        lines.append(
            f"**{i}. {item['title'][:120]}**\n"
            f"　　{item.get('uploader', '未知')} · {format_duration(item.get('duration'))}"
        )
    card.append(Module.Section(Element.Text("\n".join(lines), Types.Text.KMD)))

    # action-group 最多 4 个按钮，SEARCH_LIMIT=5 会直接被 KOOK 拒收（40000），
    # 所以拆成多组。
    buttons = [
        Element.Button(str(i + 1), f"search:pick:{search_id}:{i}",
                       Types.Click.RETURN_VAL, Types.Theme.PRIMARY)
        for i in range(len(results))
    ]
    for i in range(0, len(buttons), MAX_BUTTONS_PER_GROUP):
        card.append(Module.ActionGroup(*buttons[i:i + MAX_BUTTONS_PER_GROUP]))
    return CardMessage(card)


# ---------------- 播放 ----------------

def now_playing_card(song: dict, thumbnail: str = None) -> CardMessage:
    card = Card(theme=Types.Theme.INFO)
    card.append(Module.Header(song['title'][:100]))
    body = kv(
        ("上传者", song.get('uploader', '未知')),
        ("时长", format_duration(song.get('duration'))),
    )
    if thumbnail:
        card.append(Module.Section(
            Element.Text(body, Types.Text.KMD),
            accessory=Element.Image(thumbnail, size=Types.Size.SM),
            mode=Types.SectionMode.RIGHT,
        ))
    else:
        card.append(Module.Section(Element.Text(body, Types.Text.KMD)))
    return CardMessage(card)


def queue_card(current: dict, queued: list, limit_chars: int) -> CardMessage:
    card = Card(theme=Types.Theme.PRIMARY)
    card.append(Module.Header("📋 播放列表"))
    lines = []
    total = 0
    if current:
        line = f"▶️ **当前**: {current.get('title', '未知')}"
        lines.append(line)
        total += len(line) + 1
    for i, song in enumerate(queued, 1):
        line = f"{i}. {song.get('title', '未知')}"
        if total + len(line) + 1 > limit_chars:
            lines.append("...(内容过长，已截断)")
            break
        lines.append(line)
        total += len(line) + 1
    card.append(Module.Section(Element.Text("\n".join(lines) or "📭 队列为空。", Types.Text.KMD)))
    return CardMessage(card)


# ---------------- 提醒 ----------------

def reminders_card(rows) -> CardMessage:
    from core.db import format_remaining
    card = Card(theme=Types.Theme.WARNING)
    card.append(Module.Header("⏰ 你的提醒"))
    card.append(Module.Section(Element.Text(
        "用 `/cancelreminder <编号>` 可以取消喵。", Types.Text.KMD)))
    card.append(Module.Divider())
    now = datetime.now().timestamp()
    lines = []
    for row in rows:
        due_local = datetime.fromtimestamp(row['due_ts'], CST).strftime('%m-%d %H:%M')
        remaining = format_remaining(row['due_ts'] - now)
        lines.append(f"**#{row['id']}** · {due_local}（还有 {remaining}）\n　　{row['text'][:200]}")
    card.append(Module.Section(Element.Text("\n".join(lines), Types.Text.KMD)))
    return CardMessage(card)


# ---------------- 帮助 ----------------

HELP_SECTIONS = [
    ("🎵 音乐播放",
     "`/play <链接>` – 让塔菲在语音频道放歌，只收 YouTube / B站链接。\n"
     "`/search <关键词> [bilibili]` – 搜出 5 个结果，点编号按钮选一首，不填平台默认 YouTube。\n"
     "`/skip` – 跳过当前这首歌。\n"
     "`/queue` – 查看播放列表。\n"
     "`/stop` – 停止播放、清空队列、退出语音频道。\n"
     "`/nowplaying` – 当前播放的歌曲详情。\n"
     "（`/pause`、`/resume` 在 KOOK 上暂时做不到，原因塔菲会在指令里说明喵）"),
    ("🎮 组队开黑",
     "`/party <游戏名> [人数上限]` – 建一个组队房间，其他人点按钮加入/退出。\n"
     "• 队长退出会自动解散。\n"
     "• 满员或 60 秒超时自动关闭并通知全体。\n"
     "• 组队状态存在数据库里，塔菲重启后按钮还能用喵！"),
    ("💰 游戏折扣",
     "`/deal <游戏名>` – 搜某个游戏当前的折扣，最多 5 条。\n"
     "`/hotdeals` – 当前热门折扣 Top 10，已经帮你过滤了：\n"
     "　• 折扣大于 40%\n"
     "　• Steam 评价 Positive 或 Very Positive\n"
     "　• 评价数不少于 100\n"
     "每天 22:00 会自动播报，而且播过的不会重复播喵。"),
    ("☁️ 天气预报",
     "`/sky <城市名> [国家代码]` – 查实时天气。\n"
     "支持中文，例如 `/sky 北京` 或 `/sky Tokyo JP`。\n"
     "每天上午 10 点自动播报关注城市的天气。"),
    ("📊 投票",
     "`/poll <问题> <选项1> <选项2>` – 发起二选一按钮投票，60 秒后结束。\n"
     "注意：三个参数用空格分开，带空格的内容请用引号包起来喵。"),
    ("⏰ 提醒",
     "`/remind <多久> <内容>` – 到点塔菲来叫你，例如 `/remind 2h 记得吃饭`。\n"
     "支持 `30m` `2h` `1d` `1小时30分`，最长一年。\n"
     "`/reminders` – 看看自己还有哪些提醒。\n"
     "`/cancelreminder <编号>` – 取消某一条。\n"
     "提醒存在数据库里，塔菲重启也不会忘喵！"),
    ("✨ 其他互动",
     "`/ping` – 看看塔菲的网速。\n"
     "`/dog @某人` – 随机一句舔狗文案并 @ 他。\n"
     "• 关键词触发：消息里有「死」→ 好似喵；有「关注」→ 关注塔菲喵。\n"
     "• 随机插嘴：没 @ 塔菲时有 15% 概率冒泡。\n"
     "• @塔菲 或私聊时走 AI 回复，可以发图片给塔菲看喵！"),
]


def help_card(bot_avatar: str = None) -> CardMessage:
    card = Card(theme=Types.Theme.SECONDARY)
    card.append(Module.Header("🎀 永雏塔菲 Bot 使用手册"))
    intro = ("你好呀，我是塔菲！雏草姬最爱的小Taffy！\n"
             "下面就是咱所有的本领，学会就能一起愉快玩耍啦～")
    if bot_avatar:
        card.append(Module.Section(
            Element.Text(intro, Types.Text.KMD),
            accessory=Element.Image(bot_avatar, size=Types.Size.SM),
            mode=Types.SectionMode.RIGHT,
        ))
    else:
        card.append(Module.Section(Element.Text(intro, Types.Text.KMD)))

    for title, body in HELP_SECTIONS:
        card.append(Module.Divider())
        card.append(Module.Section(Element.Text(f"**{title}**\n{body}", Types.Text.KMD)))

    card.append(Module.Divider())
    card.append(Module.Section(Element.Text(
        "**💡 小贴士**\n"
        "• KOOK 没有斜杠指令补全，记不住就再 `/help` 一次喵。\n"
        "• 参数用空格分开，带空格的内容用引号包起来。\n"
        "• 塔菲不理你的话，检查一下咱在这个频道的权限。",
        Types.Text.KMD)))
    card.append(Module.Context(Element.Text("有任何问题随时 @塔菲 问喵！", Types.Text.KMD)))
    return CardMessage(card)

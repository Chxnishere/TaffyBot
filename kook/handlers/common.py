"""各 handler 共用的小工具。

回复用 msg.reply(x)，只给发起者看的加 is_temp=True，
就地改卡片用 update_card(bot, msg_id, card)。
"""
import io
import json
import logging
import re

import aiohttp
from khl import MessageTypes, api

from config import MAX_REPLY_CHARS

logger = logging.getLogger(__name__)


def rest(args) -> str:
    """把 *args 重新拼回一句话。

    khl.py 的默认 lexer 按空格切词，而 /play、/deal、/search 的参数
    都是带空格的自由文本。不写这个函数就会在六个地方各踩一次同样的坑。
    """
    return " ".join(str(a) for a in args).strip()


# KOOK 会把用户粘进来的链接自动转成 KMarkdown 的 [文字](链接) 形式，
# 所以 msg.content 里拿到的不是裸 URL。yt-dlp 收到整串 [..](..) 就会报
# "is not a valid URL"。这两个正则负责把它还原回来。
_KMD_LINK = re.compile(r'\[([^\]]*)\]\(\s*(https?://[^\s)]+)\s*\)')
_ANGLE_URL = re.compile(r'<\s*(https?://[^\s>]+)\s*>')


def clean_query(text: str) -> str:
    """把用户输入里的链接语法还原成裸 URL。

    处理三种情况：
      [https://x](https://x)  -> https://x      （KOOK 自动转的）
      [看这个](https://x)      -> https://x      （手动写的 markdown 链接）
      <https://x>             -> https://x
    不是链接的普通搜索词原样返回。
    """
    if not text:
        return ''
    text = text.strip()
    text = _KMD_LINK.sub(lambda m: m.group(2), text)
    text = _ANGLE_URL.sub(lambda m: m.group(1), text)
    return text.strip()


async def safe_reply(msg, text: str, is_temp: bool = False):
    """先按 KMarkdown 发，被 KOOK 拒收就退回纯文本重发一次。

    用在内容不可控的地方（AI 回复、外部 API 返回的文案）：
    里面可能有落单的方括号、星号或链接，KMarkdown 校验不过就整条发不出去。
    """
    text = truncate(text)
    try:
        return await msg.reply(text, is_temp=is_temp)
    except Exception as e:
        if '40000' not in str(e):
            raise
        logger.warning(f"KMarkdown 被拒，改用纯文本重发: {e}")
        return await msg.reply(text, type=MessageTypes.TEXT, is_temp=is_temp)


async def reply_plain(msg, text: str, is_temp: bool = False):
    """用纯文本发，不走 KMarkdown。

    报错信息里经常带着用户原样的输入（链接、方括号、星号），
    当成 KMarkdown 发出去会被 KOOK 拒收：
    "40000 markdown_string不存在或者你没有权限操作"，
    于是连"出错了"这句话都发不出去。
    """
    return await msg.reply(truncate(text), type=MessageTypes.TEXT, is_temp=is_temp)


def mention(user_id: str) -> str:
    return f"(met){user_id}(met)"


def truncate(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...（太长了喵！）"


# ---------------- 发送 / 更新 ----------------

def check_card(card) -> list:
    """发之前自查 KOOK 的卡片限制。

    KOOK 只会回一句 "40000 卡片消息json没有通过验证或者不存在"，
    不告诉你是哪一条规则，所以在本地先查一遍，日志里说人话。
    """
    problems = []
    try:
        data = json.loads(json.dumps(card, ensure_ascii=False))
    except Exception as e:
        return [f'卡片无法序列化: {e}']
    if not isinstance(data, list):
        return ['卡片消息最外层必须是数组']
    if len(data) > 5:
        problems.append(f'卡片数 {len(data)} > 5')
    total = 0
    for ci, c in enumerate(data):
        mods = c.get('modules', [])
        total += len(mods)
        for m in mods:
            if m.get('type') == 'action-group' and len(m.get('elements', [])) > 4:
                problems.append(f'第{ci + 1}个卡片的 action-group 有 '
                                f'{len(m["elements"])} 个按钮 > 4')
            if m.get('type') == 'header':
                txt = m.get('text', {})
                content = txt.get('content', '') if isinstance(txt, dict) else str(txt)
                if len(content) > 100:
                    problems.append(f'第{ci + 1}个卡片的 header 有 {len(content)} 字 > 100')
    if total > 50:
        problems.append(f'模块总数 {total} > 50')
    return problems


async def send_card(bot, channel_id: str, card, temp_target_id: str = ''):
    """按频道 id 发卡片。返回 API 响应，里面有 msg_id。"""
    problems = check_card(card)
    if problems:
        logger.error("卡片不合法，KOOK 会拒收: " + "; ".join(problems))
    params = {
        'type': MessageTypes.CARD.value,
        'target_id': str(channel_id),
        'content': json.dumps(card, ensure_ascii=False),
    }
    if temp_target_id:
        params['temp_target_id'] = str(temp_target_id)
    return await bot.client.gate.exec_req(api.Message.create(**params))


async def send_text(bot, channel_id: str, text: str, temp_target_id: str = '',
                    plain: bool = False):
    """plain=True 用纯文本发，适合内容里可能带用户输入的报错。"""
    params = {
        'type': MessageTypes.TEXT.value if plain else MessageTypes.KMD.value,
        'target_id': str(channel_id),
        'content': truncate(text),
    }
    if temp_target_id:
        params['temp_target_id'] = str(temp_target_id)
    return await bot.client.gate.exec_req(api.Message.create(**params))


async def update_card(bot, msg_id: str, card):
    """就地改一条卡片消息。"""
    return await bot.client.gate.exec_req(api.Message.update(
        msg_id=str(msg_id),
        content=json.dumps(card, ensure_ascii=False),
    ))


async def dm(bot, user_id: str, content):
    """私信。KOOK 的私聊走独立的 API 路径，不是频道的一个变体。"""
    try:
        user = await bot.client.fetch_user(str(user_id))
        await user.send(content)
        return True
    except Exception as e:
        logger.debug(f"私信 {user_id} 失败: {e}")
        return False


# ---------------- 资源上传 ----------------

_asset_cache = {}


async def upload_external(bot, url: str):
    """把外站图片搬到 KOOK 的资源服务器。

    KOOK 的卡片不会渲染任意外链图片，B站/YouTube 封面、天气图标都要先传。
    同一个 url 只传一次，缓存住，不然每次 /sky 都白传一遍。
    """
    if not url:
        return None
    if url in _asset_cache:
        return _asset_cache[url]
    # KOOK 自家的图本来就能显示，不用绕一圈
    if 'kookapp.cn' in url or 'kaiheila.cn' in url:
        _asset_cache[url] = url
        return url
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
        buf = io.BytesIO(data)
        buf.name = url.split('/')[-1].split('?')[0] or 'image.png'
        kook_url = await bot.client.create_asset(buf)
        _asset_cache[url] = kook_url
        return kook_url
    except Exception as e:
        logger.debug(f"上传外部图片失败 {url}: {e}")
        return None


# ---------------- 语音频道查询 ----------------

async def find_user_voice_channel(bot, guild_id: str, user_id: str):
    """用户当前在哪个语音频道。

    KOOK 没有本地缓存，要打一次 API。
    """
    try:
        data = await bot.client.gate.exec_req(api.ChannelUser.getJoinedChannel(
            page=1, page_size=10, guild_id=str(guild_id), user_id=str(user_id),
        ))
        items = data.get('items') if isinstance(data, dict) else None
        if items:
            return str(items[0]['id'])
    except Exception as e:
        logger.debug(f"查询 {user_id} 的语音频道失败: {e}")
    return None


async def voice_channel_humans(bot, channel_id: str, bot_user_id: str):
    """语音频道里除了机器人还有几个人。查不出来返回 None。

    KOOK 每次都要打 API，所以 idle 检查的轮询间隔别设太短。

    "查不出来"和"0 个人"必须分开：混在一起的话接口一抖，
    塔菲就会在有人听歌的时候说一句"语音频道没人了"然后退出。
    """
    try:
        data = await bot.client.gate.exec_req(api.Channel.userList(channel_id=str(channel_id)))
        if not isinstance(data, list):
            logger.warning(f"查询语音频道 {channel_id} 人数返回了意外的内容，这一轮先不判断")
            return None
        return len([u for u in data if str(u.get('id')) != str(bot_user_id)])
    except Exception as e:
        logger.warning(f"查询语音频道 {channel_id} 人数失败，这一轮先不判断: {e}")
        return None


# ---------------- 按钮分发 ----------------
# Discord 用 View 对象把回调挂在自己身上，KOOK 只给一个全局的
# message_btn_click 事件，带一个我们自己定义的 value 字符串。
# 所以路由要自己做：value 写成 "前缀:动作:id[:参数]"，在这里按前缀分发。

_button_routes = {}


def on_button(prefix: str):
    """装饰器。注册一个按钮前缀的处理函数。

    handler 签名: async def handler(bot, event, parts) -> None
    parts 是 value 用 ':' 切开的列表。
    """
    def deco(func):
        _button_routes[prefix] = func
        return func
    return deco


async def dispatch_button(bot, event):
    body = event.body or {}
    value = body.get('value') or ''
    if not value:
        return
    parts = value.split(':')
    handler = _button_routes.get(parts[0])
    if handler is None:
        return
    try:
        await handler(bot, event, parts)
    except Exception as e:
        logger.error(f"按钮处理异常 value={value}: {e}", exc_info=e)


def button_user_id(event) -> str:
    return str((event.body or {}).get('user_id', ''))


def button_channel_id(event) -> str:
    return str((event.body or {}).get('target_id', ''))


def button_msg_id(event) -> str:
    return str((event.body or {}).get('msg_id', ''))


def button_guild_id(event) -> str:
    return str((event.body or {}).get('guild_id', '') or '')

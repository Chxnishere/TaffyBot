# TaffyBot 🎀

简体中文 | [English](README.en.md)

一个说**简体中文**的群聊机器人：能放歌、组队、查游戏折扣、报天气，被 @ 时用 AI 聊天，没人理它的时候还会自己冒泡插嘴。人设是永雏塔菲，可以换成你自己的。

同一套功能有两个版本，各自独立，选你用的平台就行：

| 版本 | 文件夹 | 平台 | 界面语言 |
|---|---|---|---|
| Discord 版 | [`discord/`](discord/) | Discord | 简体中文（默认）/ English |
| KOOK 版 | [`kook/`](kook/) | KOOK | 简体中文 |

> 这是粉丝自制的项目，和永雏塔菲本人及其所属公司没有任何关系。
> 它是为朋友之间的小服务器做的，不是面向大型公开社区的成品机器人。

## 能做什么

| 功能 | Discord | KOOK |
|---|:---:|:---:|
| AI 聊天：@ 它、私聊它，或者它随机插嘴（可以看图） | ✅ | ✅ |
| 放歌：YouTube / B站链接，带队列 | ✅ | ✅（不支持暂停） |
| 按歌名搜索再选一首 | ✅ | ✅ |
| 组队开黑，满员或超时自动通知 | ✅ | ✅（重启后按钮还能用） |
| 二选一投票 | ✅ | ✅（重启后按钮还能用） |
| 游戏折扣查询 + 每日折扣播报 | ✅ | ✅（播过的不重复播） |
| 天气查询 + 每日天气播报 | ✅ | ✅ |
| 定时提醒 | — | ✅ |
| 英文界面 | ✅ | — |

## 指令

| 指令 | 说明 |
|---|---|
| `/play <链接>` | 在你所在的语音频道放歌。只收 YouTube / B站链接 |
| `/search <关键词> [bilibili]` | 搜出 5 个结果，选一首放。不写平台默认 YouTube |
| `/skip` `/queue` `/nowplaying` `/stop` | 跳过、看队列、看当前歌曲、停止并退出语音 |
| `/pause` `/resume` | 暂停、恢复（仅 Discord） |
| `/party <游戏名> [人数上限]` | 开一个组队房间，其他人点按钮加入或退出 |
| `/poll <问题> <选项1> <选项2>` | 发起投票，60 秒后结束 |
| `/deal <游戏名>` | 查一个游戏当前的折扣 |
| `/hotdeals` | 当前热门折扣 Top 10 |
| `/sky <城市> [国家代码]` | 查实时天气 |
| `/remind <多久> <内容>` `/reminders` `/cancelreminder <编号>` | 定时提醒（仅 KOOK），例如 `/remind 2h 记得吃饭` |
| `/dog @某人` | 随机一句舔狗文案并 @ 他 |
| `/ping` `/help` | 延迟、使用手册 |

除了指令，它还会：

- 每天 10:00 播报天气、22:00 播报优质折扣（UTC+8，各自发到你指定的频道）。
- 没被 @ 的时候，大约有 15% 的概率对一条消息插嘴。
- 消息里出现「死」或「关注」时自动回一句（群里的梗，不喜欢可以在代码里删掉）。

## 需要准备什么

### 运行环境

| 需要 | 版本 / 说明 |
|---|---|
| 操作系统 | Linux 或 macOS。Windows 不能直接跑，请用 WSL |
| Python | 3.10 或更高 |
| ffmpeg | 放歌要用。KOOK 版还要求它带 libopus 编码器，自带的 `python doctor.py` 会帮你检查 |
| opus 库 | 仅 Discord 版放歌要用 |
| git | 下载代码用 |
| 一台一直开着的机器 | 机器人关机就下线，一般放在云服务器上 |
| 网络 | 要能连上 Discord 或 KOOK，以及 Google（AI 聊天）、YouTube 和 B站（放歌）。中国大陆的网络通常连不上 Discord、Google 和 YouTube，建议用海外服务器 |

Debian / Ubuntu 上一条命令装好系统依赖：

```bash
sudo apt install -y python3 python3-venv git ffmpeg libopus0
```

### Python 依赖

不用一个个装，后面的 `pip install -r requirements.txt` 会一次装齐。两个版本只有第一行不同：

| 库 | 最低版本 | 做什么 |
|---|---|---|
| [discord.py](https://github.com/Rapptz/discord.py)（带 `[voice]`） | 2.4 | Discord 版的核心，`[voice]` 是语音功能需要的部分 |
| [khl.py](https://github.com/TWT233/khl.py) | 0.3.17 | KOOK 版的核心 |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 2024.1.1 | 解析 YouTube / B站的音频。网站经常改版，放不出歌时先 `pip install -U yt-dlp` |
| [google-genai](https://github.com/googleapis/python-genai) | 1.0 | 调用 Gemini |
| [aiohttp](https://github.com/aio-libs/aiohttp) | 3.9 | 访问天气、折扣等接口 |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | 1.0 | 读取 `.env` |

### 账号和密钥

只有机器人 Token 是必须的。其他的不填，对应功能就关掉，别的照常用。

| 需要 | 用在哪 | 必须吗 | 去哪里拿 |
|---|---|---|---|
| Discord 机器人 Token | Discord 版 | 必须 | [Discord Developer Portal](https://discord.com/developers/applications)，还要打开 Message Content Intent |
| KOOK 机器人 Token | KOOK 版 | 必须 | [KOOK 开发者中心](https://developer.kookapp.cn/)，连接模式选 WebSocket |
| Gemini API key | AI 聊天 | 可选 | [Google AI Studio](https://aistudio.google.com/) |
| OpenWeatherMap API key | `/sky` 和每日天气 | 可选 | [OpenWeatherMap](https://openweathermap.org/api) |
| api.oick.cn 的 key | `/dog` | 可选 | [api.oick.cn](https://api.oick.cn/) |
| `cookies.txt` | 放歌 | 强烈建议 | 自己从浏览器导出，见[关于 cookies.txt](#关于-cookiestxt) |
| 播报频道的 ID | 每日天气、每日折扣 | 可选 | 在客户端里打开开发者模式，右键频道复制 ID |

游戏折扣用的是 [CheapShark](https://www.cheapshark.com/) 的公开接口，不需要 key。各家接口的免费额度和收费方式以它们官网为准。

## 安装

### Discord 版

1. 在 [Discord Developer Portal](https://discord.com/developers/applications) 新建应用，进 **Bot** 页面复制 Token，并打开 **Message Content Intent**。
2. 在 **OAuth2** 页面生成邀请链接：范围勾 `bot` 和 `applications.commands`；权限勾发送消息、嵌入链接、读取消息历史、添加反应、连接、说话。想让组队通知和每日播报真的 @ 到全体，再勾上「提及 @everyone」。用这个链接把机器人拉进你的服务器。
3. 下载代码并安装依赖：

   ```bash
   git clone https://github.com/Chxnishere/TaffyBot.git
   cd TaffyBot/discord
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```

4. 打开 `.env`，至少填上 `DISCORD_TOKEN`。其余设置见下面的表格。
5. 想放歌的话，准备 `cookies.txt`（见[关于 cookies.txt](#关于-cookiestxt)），放在 `discord/` 文件夹里。
6. 启动。要在 `discord/` 文件夹里启动，它按当前目录找人设文件和 cookies：

   ```bash
   python main.py
   ```

   日志里出现「启动健康报告」就是起来了，报告会列出每个功能有没有配好。斜杠指令默认是全局同步，最多要等 1 小时才出现；在 `.env` 里填上 `GUILD_ID` 可以立刻生效。

### KOOK 版

1. 在 [KOOK 开发者中心](https://developer.kookapp.cn/) 新建机器人，连接模式选 **WebSocket**，复制 Token，再把机器人邀请进你的服务器。
2. 下载代码并安装依赖：

   ```bash
   git clone https://github.com/Chxnishere/TaffyBot.git
   cd TaffyBot/kook
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```

3. 打开 `.env`，至少填上 `KOOK_TOKEN`。
4. 想放歌的话，把 `cookies.txt` 放在 `kook/` 文件夹里。KOOK 语音需要 ffmpeg 带 libopus 编码器。
5. 先跑一遍自检，它会告诉你哪里没配好、该怎么改：

   ```bash
   python doctor.py
   ```

6. 自检没有 ❌ 之后启动：

   ```bash
   python main.py
   ```

### 关于 cookies.txt

YouTube 和 B站经常拒绝没有登录状态的请求，所以放歌功能需要一份浏览器导出的登录 cookie：

1. 在浏览器里登录 YouTube 和 B站。
2. 用能导出 **Netscape 格式** `cookies.txt` 的浏览器扩展把 cookie 导出来。
3. 把文件命名为 `cookies.txt`，放进对应版本的文件夹。

⚠️ 这个文件等于你的账号登录凭证，拿到它的人可以直接登录你的账号。建议专门注册小号来导出，不要发给任何人，也不要提交到 git（仓库的 `.gitignore` 已经把它挡住了）。

## 设置

所有设置都写在 `.env` 里，每一项在 `.env.example` 里都有注释。

| 设置 | 默认值 | 说明 |
|---|---|---|
| `DISCORD_TOKEN` / `KOOK_TOKEN` | — | 机器人 Token，必填 |
| `GEMINI_API_KEY` | 空 | Gemini 的 key。不填就没有 AI 聊天 |
| `GEMINI_MODEL` | `gemini-3.6-flash` | 用哪个模型（仅 KOOK 版可改） |
| `OWM_API_KEY` | 空 | OpenWeatherMap 的 key。不填就没有天气 |
| `DOG_API_KEY` | 空 | `/dog` 用的 api.oick.cn key |
| `GENERAL_ID` | 空 | 每日天气发到哪个频道（频道 ID） |
| `LOOT_ID` | 空 | 每日折扣发到哪个频道（频道 ID） |
| `GUILD_ID` | 空 | 仅 Discord。填服务器 ID 后指令立刻生效 |
| `LANGUAGE` | `zh` | 仅 Discord。`zh` 简体中文，`en` 英文 |
| `WEATHER_CITIES` | `Shanghai,CN;New York,US;Tokyo,JP` | 每日播报哪些城市，格式是 `城市,国家代码;城市,国家代码` |

用量上限。默认值给得很宽，小服务器基本碰不到；填 `0` 就是关掉这一项：

| 设置 | 默认值 | 说明 |
|---|---|---|
| `MAX_QUEUE` | `50` | 每个服务器最多排多少首歌 |
| `MAX_REMINDERS_PER_USER` | `20` | 仅 KOOK。每人最多挂多少条提醒 |
| `MAX_IMAGE_MB` | `8` | 发给 AI 的单张图片大小上限 |
| `MAX_IMAGES_PER_MESSAGE` | `4` | 一条消息最多带几张图给 AI |
| `AI_COOLDOWN_SECONDS` | `3` | 同一个人两次 AI 回复之间至少隔几秒 |
| `AI_DAILY_LIMIT` | `0` | 每天最多调多少次 AI。`0` 是不限，怕账单失控可以设一个数 |

## 用之前要知道的事

- **消息会发给 Google。** 被 @、被私聊，以及随机插嘴的时候，那条消息的文字和图片会发给 Google Gemini 来生成回复。随机插嘴意味着没有叫机器人的人，消息也可能被发过去。不填 `GEMINI_API_KEY` 就完全不会发。聊天上下文只放在内存里，大约一小时没动静就清掉。
- **它会 @全体。** 组队结束和每日播报都会 @全体成员，AI 的回复也可能被人引导着带上 @全体。不想要的话，在服务器设置里关掉机器人「提及 @everyone」的权限。
- **放歌请自己把关。** 通过机器人播放 YouTube / B站的内容是否符合这些网站的服务条款和你所在地的法律，需要你自己确认。
- **只跑一个实例。** 同一个 Token 同时跑两个进程会重复回复，程序启动时会自己检查并拒绝第二个。

## 换成自己的人设

人设就是三个 Markdown 文件：`persona.md`、`distillation.md`、`self-reference.md`。Discord 版放在 `discord/` 下，KOOK 版放在 `kook/skills/` 下。改掉里面的内容再重启，机器人就换了性格。

## AI 使用说明
这个项目的一部分代码和文档是借助 AI 工具写的，发布前由作者检查&测试过。

## 致谢

- 人设文件来自 [ly-xxx/ace-taffy-skill](https://github.com/ly-xxx/ace-taffy-skill)（MIT 许可），许可声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
- [discord.py](https://github.com/Rapptz/discord.py)、[khl.py](https://github.com/TWT233/khl.py)、[yt-dlp](https://github.com/yt-dlp/yt-dlp)、[google-genai](https://github.com/googleapis/python-genai)。
- 折扣数据来自 [CheapShark](https://www.cheapshark.com/)，天气数据来自 [OpenWeatherMap](https://openweathermap.org/)。

## 许可

本项目以 [GPL-3.0](LICENSE) 许可发布：你可以自由使用、修改和分享，但分享修改后的版本时也必须以 GPL-3.0 开源。人设文件保留其原有的 MIT 许可。

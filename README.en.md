# TaffyBot 🎀

[简体中文](README.md) | English

A group-chat bot built **Simplified Chinese first**: it plays music, sets up game parties, finds game deals, reports the weather, chats with AI when mentioned, and chimes in on its own when nobody is talking to it. The persona is the VTuber Ace Taffy (永雏塔菲), and you can swap in your own.

There are two independent versions with the same features. Pick the one for your platform:

| Version | Folder | Platform | Interface language |
|---|---|---|---|
| Discord | [`discord/`](discord/) | Discord | Simplified Chinese (default) / English |
| KOOK | [`kook/`](kook/) | KOOK | Simplified Chinese |

This guide covers the Discord version. The KOOK version is Chinese-only; its setup is in the [Chinese README](README.md).

> This is a fan-made project. It is not affiliated with Ace Taffy or her agency.
> It was built for small servers among friends, not as a finished bot for large public communities.

## What it does

- **AI chat.** Mention it or DM it for a reply, with image support. It also joins the conversation unprompted on roughly 15% of messages.
- **Music.** Plays YouTube and Bilibili links in voice, with a queue, pause and skip.
- **Search.** Look a song up by name and pick from five results.
- **Parties.** Open a party room for a game; others join with a button, and everyone is notified when it fills or times out.
- **Polls.** Two-option button polls that close after 60 seconds.
- **Game deals.** Look up deals for a game, see the top 10, and get a daily post of the best ones.
- **Weather.** Live weather for any city, plus a daily post for the cities you choose.

## Commands

| Command | What it does |
|---|---|
| `/play <link>` | Plays in your voice channel. YouTube and Bilibili links only |
| `/search <keywords> [platform]` | Shows 5 results to pick from. Defaults to YouTube |
| `/skip` `/queue` `/nowplaying` `/stop` | Skip, show the queue, show the current song, stop and leave voice |
| `/pause` `/resume` | Pause and resume |
| `/party <game> [player limit]` | Opens a party room that others join or leave with a button |
| `/poll <question> <option1> <option2>` | Starts a poll that closes after 60 seconds |
| `/deal <game>` | Current deals for one game |
| `/hotdeals` | Top 10 deals right now |
| `/sky <city> [country code]` | Live weather |
| `/dog <user>` | Sends someone a random simp line (the lines are in Chinese) |
| `/ping` `/help` | Latency, user guide |

It also does a few things on its own:

- Posts the weather at 10:00 and the best deals at 22:00 every day (UTC+8), each to a channel you choose.
- Replies with a canned line to messages containing 「死」 or 「关注」. These are Chinese inside jokes; delete them in the code if you don't want them.

Slash-command descriptions follow each user's own Discord language automatically, in Chinese or English.

## What you need

### Environment

| Requirement | Version / notes |
|---|---|
| Operating system | Linux or macOS. It does not run on Windows directly; use WSL |
| Python | 3.10 or newer |
| ffmpeg | Needed for music |
| opus library | Needed for music |
| git | To download the code |
| A machine that stays on | The bot goes offline when it stops, so it usually lives on a cloud server |
| Network | Must reach Discord, Google (AI chat), and YouTube and Bilibili (music). Networks in mainland China usually can't reach Discord, Google or YouTube |

On Debian or Ubuntu, one command installs the system packages:

```bash
sudo apt install -y python3 python3-venv git ffmpeg libopus0
```

### Python packages

You don't install these one by one; `pip install -r requirements.txt` in the steps below installs them all.

| Package | Minimum version | What it's for |
|---|---|---|
| [discord.py](https://github.com/Rapptz/discord.py) (with `[voice]`) | 2.4 | The Discord library. `[voice]` adds what voice playback needs |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 2024.1.1 | Resolves YouTube and Bilibili audio. Sites change often; if music stops working, try `pip install -U yt-dlp` first |
| [google-genai](https://github.com/googleapis/python-genai) | 1.0 | Calls Gemini |
| [aiohttp](https://github.com/aio-libs/aiohttp) | 3.9 | Calls the weather and deals services |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | 1.0 | Reads `.env` |

### Accounts and keys

Only the bot token is required. Leave any of the others empty and that feature turns off while everything else keeps working.

| What | Used for | Required? | Where to get it |
|---|---|---|---|
| Discord bot token | The bot itself | Required | [Discord Developer Portal](https://discord.com/developers/applications). Message Content Intent must be turned on |
| Gemini API key | AI chat | Optional | [Google AI Studio](https://aistudio.google.com/) |
| OpenWeatherMap API key | `/sky` and the daily weather post | Optional | [OpenWeatherMap](https://openweathermap.org/api) |
| api.oick.cn key | `/dog` | Optional | [api.oick.cn](https://api.oick.cn/) |
| `cookies.txt` | Music | Strongly recommended | Export it from your browser; see [About cookies.txt](#about-cookiestxt) |
| Channel IDs | Daily weather and daily deals posts | Optional | Turn on Developer Mode in Discord, then right-click a channel and copy its ID |

Game deals use the public [CheapShark](https://www.cheapshark.com/) service, which needs no key. Check each provider's own site for its free allowance and pricing.

## Setup

### Steps

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications). On the **Bot** page, copy the token and turn on **Message Content Intent**.
2. On the **OAuth2** page, generate an invite link with the scopes `bot` and `applications.commands`, and these permissions: Send Messages, Embed Links, Read Message History, Add Reactions, Connect, Speak. Add Mention Everyone if you want party results and the daily posts to actually ping everyone. Use the link to add the bot to your server.
3. Get the code and install the dependencies:

   ```bash
   git clone https://github.com/Chxnishere/TaffyBot.git
   cd TaffyBot/discord
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```

4. Open `.env` and set at least `DISCORD_TOKEN`. Add `LANGUAGE=en` for English. The other settings are in the tables below.
5. For music, prepare a `cookies.txt` (see [About cookies.txt](#about-cookiestxt)) and put it in the `discord/` folder.
6. Start it from inside the `discord/` folder, because it looks for the persona files and cookies in the current directory:

   ```bash
   python main.py
   ```

   When the log prints the startup health report, the bot is running; the report lists which features are configured. Slash commands sync globally by default, which can take up to an hour to appear. Set `GUILD_ID` in `.env` to make them appear at once.

### About cookies.txt

YouTube and Bilibili often refuse requests that aren't logged in, so music needs login cookies exported from a browser:

1. Log in to YouTube and Bilibili in your browser.
2. Export the cookies with a browser extension that writes a **Netscape-format** `cookies.txt`.
3. Name the file `cookies.txt` and put it in the `discord/` folder.

⚠️ This file is your account login. Anyone who has it can sign in as you. Use throwaway accounts for it, never share it, and never commit it to git (the repo's `.gitignore` already blocks it).

## Settings

All settings live in `.env`, and each one is commented in `.env.example`.

| Setting | Default | What it does |
|---|---|---|
| `DISCORD_TOKEN` | — | The bot token. Required |
| `GEMINI_API_KEY` | empty | Gemini key. Without it there is no AI chat |
| `OWM_API_KEY` | empty | OpenWeatherMap key. Without it there is no weather |
| `DOG_API_KEY` | empty | Key for api.oick.cn, used by `/dog` |
| `GENERAL_ID` | empty | Channel ID for the daily weather post |
| `LOOT_ID` | empty | Channel ID for the daily deals post |
| `GUILD_ID` | empty | Your server ID. Makes slash commands appear at once |
| `LANGUAGE` | `zh` | `zh` for Simplified Chinese, `en` for English |
| `WEATHER_CITIES` | `Shanghai,CN;New York,US;Tokyo,JP` | Cities for the daily weather post, as `City,CC;City,CC` |

Usage limits. The defaults are generous enough that a small server won't notice them; set one to `0` to turn it off:

| Setting | Default | What it caps |
|---|---|---|
| `MAX_QUEUE` | `50` | Songs queued per server |
| `MAX_IMAGE_MB` | `8` | Size of one image sent to the AI |
| `MAX_IMAGES_PER_MESSAGE` | `4` | Images per message sent to the AI |
| `AI_COOLDOWN_SECONDS` | `3` | Seconds between AI replies to the same person |
| `AI_DAILY_LIMIT` | `0` | AI calls per day. `0` means unlimited; set a number to cap your bill |

## Things to know before you use it

- **Messages go to Google.** When the bot is mentioned, DMed, or chimes in on its own, that message's text and images are sent to Google Gemini to generate the reply. Because of the random chime-ins, this includes messages from people who never addressed the bot. Leave `GEMINI_API_KEY` empty and nothing is sent. Chat context is kept in memory only and dropped after about an hour of inactivity.
- **It pings @everyone.** Party results and the daily posts mention everyone, and a user can also talk the AI into including @everyone in a reply. To stop this, remove the bot's "Mention @everyone" permission in your server settings.
- **Music is your responsibility.** Check for yourself whether playing YouTube or Bilibili content through a bot is allowed by those sites' terms and by the law where you live.
- **Run one instance.** Two processes on the same token would both reply; the bot checks at startup and refuses to start a second one.

## Use your own persona

The persona is three Markdown files in `discord/`: `persona.md`, `distillation.md` and `self-reference.md`. Rewrite them and restart, and the bot has a different personality. They are written in Chinese; in English mode the bot follows them but replies in English.

## AI disclosure

Parts of this project's code and documentation were written with the help of AI tools and reviewed & tested by the author before release.

## Credits

- The persona files come from [ly-xxx/ace-taffy-skill](https://github.com/ly-xxx/ace-taffy-skill) (MIT license). The license notice is in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- [discord.py](https://github.com/Rapptz/discord.py), [khl.py](https://github.com/TWT233/khl.py), [yt-dlp](https://github.com/yt-dlp/yt-dlp), [google-genai](https://github.com/googleapis/python-genai).
- Deal data from [CheapShark](https://www.cheapshark.com/), weather data from [OpenWeatherMap](https://openweathermap.org/).

## License

Released under [GPL-3.0](LICENSE): you may use, modify and share it freely, but if you share a modified version it must also be open-source under GPL-3.0. The persona files keep their original MIT license.

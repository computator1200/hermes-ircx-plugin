# Interactive Behaviours

Three behaviours turn the bot from a request/response responder into a real channel participant.

## 👀 Observe mode — spontaneous contribution

By default the bot only replies when addressed (`require_mention`). With observe mode it *watches* the channel and may occasionally contribute on its own.

```bash
IRCX_OBSERVE_MODE=true
IRCX_SPONTANEOUS_PROBABILITY=0.15     # 0..1
IRCX_SPONTANEOUS_COOLDOWN=90          # seconds between spontaneous posts per channel
IRCX_CONTEXT_BUFFER=15                # recent lines retained per channel
```

How it works:
- **Every** channel line (addressed or not) is appended to a rolling per-channel buffer and attached to the next turn as `channel_context`, so replies are conversation-aware.
- For an **unaddressed** line, the bot contributes only when `random() < spontaneous_probability` **and** at least `spontaneous_cooldown` seconds have elapsed since its last spontaneous post in that channel.
- When it does engage, it's prompted that it may stay silent — replying with exactly `<silent>` is suppressed (nothing is sent).
- **Addressed** messages always get a reply regardless of probability.

> Tip: start low (`0.1–0.2`). The cooldown is your spam guard.

## 🛠️ Runtime channel agency

Give the agent tools to manage channels on request. Disabled by default.

```bash
IRCX_ALLOW_AGENT_JOIN=true
IRCX_JOINABLE_CHANNELS=#ops,#help     # allowlist; empty = any channel
```

Tools (toolset `ircx`; enable it for the platform via `platform_toolsets.ircx`):

| Tool | What it does |
|---|---|
| `irc_join` | Join a channel (optional key). Gated by `allow_agent_join` + `joinable_channels`. Joined channels **persist across reconnects**. |
| `irc_part` | Leave a channel. |
| `irc_say` | Send to a channel the bot is in, or DM a nick. |
| `irc_list_channels` | List joined channels + current nick. |
| `irc_channel_info` | Live roster of a channel: member list (`@` ops, `+` voiced), user/op/voice counts, and the topic. Answers "who's here", "how many ops", "what's the topic". |
| `irc_whois` | What the bot knows about a user it shares a channel with: nick, ident/host, verified account, away status, shared channels. |

> `irc_channel_info` / `irc_whois` are **read-only** (no `allow_agent_join` needed) — they just report what the bot already sees via the IRCv3 state machine (NAMES/WHO/MODE/TOPIC). The bot must be a *member* of a channel to see its roster.

**Security:** because the agent acts for whoever messaged it, keep `IRCX_ALLOWED_USERS` tight when enabling agent-join so only trusted operators can direct joins. `irc_say` only targets channels the bot has actually joined.

## 💾 Downtime context persistence

IRC is stateless, but **the agent's own conversation memory persists** in Hermes' state DB across reconnects and restarts. What's missed is channel traffic *while the bot is offline*. Two mechanisms close that gap:

### IRCv3 `draft/chathistory`
On (re)join, IRCX requests `CHATHISTORY LATEST <chan> * <limit>` where the server supports it (e.g. Ergo, soju). Replayed backlog is fed into the context buffer and **never re-answered**.

```bash
IRCX_CHATHISTORY_LIMIT=50
```

### On-disk logging + tail replay
Set a log dir and IRCX appends every channel line (with `server-time`); on (re)join it replays the tail to seed the context buffer — continuity even on servers without chathistory.

```bash
IRCX_LOG_DIR=~/.hermes/logs/ircx
```

### Bouncer (strongest)
Run a **ZNC / soju** bouncer in front: point `IRCX_SERVER`/`IRCX_PORT` at the bouncer and it buffers + replays missed traffic 24/7 — no code changes. Combined with Hermes' own persistence, reconnects are near-seamless.

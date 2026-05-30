# IRCX — IRCv3 platform adapter for Hermes Agent

> **Advanced features** (see "Interactive agent behaviours" below): observe-mode
> spontaneous contribution, runtime channel-agency tools (`irc_join` / `irc_part`
> / `irc_say` / `irc_list_channels`), and downtime context persistence
> (IRCv3 `draft/chathistory` backfill + on-disk channel logging with tail replay).

A drop-in gateway platform plugin that connects Hermes to IRC with full
IRCv3 support. Built on the [`irctokens`](https://pypi.org/project/irctokens/)
+ [`ircstates`](https://pypi.org/project/ircstates/) stack — the spec-compliant,
sans-IO IRCv3 parsing/state libraries.

It re-implements, and is a superset of, the bundled
stdlib `irc` example plugin, and reaches feature parity with (and beyond)
the [OpenClaw IRC channel](https://docs.openclaw.ai/channels/irc).

> Platform name: **`ircx`**. The bundled stdlib example (`irc`) is left
> untouched so you can run them side by side and switch over when ready.

## Features

| Capability | Detail |
|---|---|
| IRCv3 CAP negotiation | `CAP LS 302` → `REQ` (offered ∩ desired) → `END`; honours `cap-notify` `NEW`/`DEL` |
| SASL | `PLAIN`, `EXTERNAL` (CertFP), `SCRAM-SHA-256`, `SCRAM-SHA-512` |
| NickServ | Automatic `IDENTIFY` fallback when SASL is not configured |
| Verified-account auth | Authorize by network-verified account (`account-tag` / `extended-join`), **not** the spoofable nick. Bare-nick auth is opt-in via `dangerously_allow_name_matching` |
| Message tags | `server-time` (accurate timestamps), `msgid`, `+draft/reply` threaded replies, `+typing` notifications |
| State tracking | `ircstates` ISUPPORT-aware casemapping, `CHANTYPES`, channels/users/accounts |
| Multi-channel | Multiple channels, per-channel keys (`#ops secret`) |
| Access control | `group_policy` (allowlist/open), per-channel + DM allowlists, per-channel tool scoping |
| Mention gating | Global + per-channel `require_mention` (OpenClaw `requireMention`) |
| Robustness | Outbound token-bucket flood protection, keepalive + ping-timeout detection, retryable-failure signalling for the gateway's background reconnect watcher (rejoins + re-auths on reconnect) |
| CTCP | Replies to `VERSION`/`PING`/`TIME`/`CLIENTINFO`/`SOURCE`; renders inbound `ACTION` |
| Safety | CRLF/NUL injection stripping on all outbound content and targets; byte-accurate, casemapping-aware message splitting |
| Cron delivery | Out-of-process `standalone_sender_fn` (ephemeral `-cron` connection) so `deliver=ircx` works when cron runs outside the gateway |

## Install

The plugin needs two small pure-Python packages:

```bash
pip install irctokens ircstates
```

If they are missing the plugin loads but disables itself (with the hint
above shown in `hermes status`). It does **not** break the gateway.

## Configuration

Configure with environment variables in `~/.hermes/.env` (recommended) or
under `gateway.platforms.ircx` in `config.yaml`. Env wins over YAML. Every
`IRCX_*` variable falls back to the legacy `IRC_*` name, so existing
example-plugin setups work unchanged.

### Quick start (env)

```bash
IRCX_SERVER=irc.libera.chat
IRCX_CHANNEL=#hermes
IRCX_NICKNAME=hermes-bot
IRCX_USE_TLS=true
# SASL (recommended over NickServ)
IRCX_SASL_MECHANISM=PLAIN
IRCX_SASL_USERNAME=hermes
IRCX_SASL_PASSWORD=...
# Access control — verified accounts allowed to command the bot
IRCX_ALLOWED_USERS=alice,bob
```

Or run `hermes gateway setup` and pick **IRC (IRCX)**.

### Full config.yaml example

```yaml
gateway:
  platforms:
    ircx:
      enabled: true
      extra:
        server: irc.libera.chat
        port: 6697
        use_tls: true
        tls_verify: true
        nickname: hermes-bot
        username: hermes
        realname: Hermes Agent
        channels:
          - "#hermes"
          - { name: "#ops", key: "s3cret" }
        sasl: { mechanism: SCRAM-SHA-512, username: hermes, password: "..." }
        # nickserv: { password: "..." }     # alternative to SASL
        require_mention: true
        group_policy: allowlist              # or "open"
        dangerously_allow_name_matching: false
        allow_from: ["alice"]                # DM allowlist (accounts)
        group_allow_from: ["alice"]          # global channel allowlist
        groups:
          "#ops":
            require_mention: false
            allow_from: ["alice"]
            tools: ["read_file", "web_search"]
            tools_by_sender:
              alice: ["*"]
        max_message_length: 450
        rate_limit: { burst: 5, per_second: 2 }
        convert_formatting: false            # true -> **md** becomes mIRC bold
```

### OpenClaw → IRCX mapping

| OpenClaw | IRCX |
|---|---|
| `host` / `port` / `tls` / `nick` | `server` / `port` / `use_tls` / `nickname` |
| `channels` | `channels` (list; inline `#chan key`) |
| `requireMention` | `require_mention` (global) + `groups.<chan>.require_mention` |
| `groupPolicy` | `group_policy` (`allowlist` / `open`) |
| `groups.<chan>.allowFrom` | `groups.<chan>.allow_from` |
| `groups.<chan>.tools` / `toolsBySender` | `groups.<chan>.tools` / `tools_by_sender` |
| `allowFrom` (DM) | `allow_from` |
| `groupAllowFrom` | `group_allow_from` |
| `dangerouslyAllowNameMatching` | `dangerously_allow_name_matching` |
| `nickserv.*` | `nickserv.*` / `IRCX_NICKSERV_*` (or prefer SASL) |

## Security notes

- **Identity is the verified account, not the nick.** With the default
  (`dangerously_allow_name_matching: false`) only users whose
  network-verified account is known (via SASL/`account-tag`) can be
  authorized. This defeats nick-spoofing on public networks. The verified
  account is what's matched against `IRCX_ALLOWED_USERS` and fed to the
  gateway's central authorization.
- Use TLS (`use_tls: true`, default) and SASL. `tls_verify: false` exists
  only for self-signed test servers.
- All outbound content and targets are stripped of CR/LF/NUL to prevent
  IRC command injection.

## Testing

```bash
python -m pytest tests/gateway/test_ircx_adapter.py -q
```

90+ network-free tests cover config precedence, channel/key/group parsing,
message splitting (byte/unicode/protocol limits), markdown handling, the
full CAP+SASL state machine, SCRAM-SHA-256/512 against a reference server
implementation, nick-collision recovery, CTCP, mention gating, the
verified-account authorization matrix, sending, typing, registration and
the standalone cron sender.

A live connection to Libera.Chat (TLS → CAP → registration → ISUPPORT →
JOIN) has been verified end to end.

## Tool scoping

Per-channel `tools` and per-sender `tools_by_sender` are **enforced** via a
small generic core hook (`MessageEvent.tool_scope` +
`gateway/run.py:_apply_tool_scope`; see `CORE_PATCH.md` at the project root).
Entries are Hermes **toolset names** (e.g. `hermes-cli`, `web`, `memory`) —
Hermes scopes tools by toolset, not individual tool name. `tools_by_sender`
(matched on the verified account/nick, case-insensitive) overrides the
channel-wide `tools`; `["*"]` means unrestricted. The narrowed toolset list
is part of the per-session agent-cache signature, so a restricted sender and
an unrestricted one in the same channel get correctly distinct agents.

> Requires the core patch in `CORE_PATCH.md`. Without it the plugin still
> works; `tool_scope` is simply ignored (scoping becomes a no-op).

## Known limitations

- Scoping is **toolset-level**, not individual-tool-level (Hermes has no
  per-tool allowlist; the agent only accepts toolset enable/disable).
- IRC has no native message **edit/delete**; `+draft/reply` is used for
  threaded replies where the server supports message tags.
- No DCC / file transfer (IRC has no first-class attachment primitive).
- `SCRAM-SHA-512` requires a server that offers it (e.g. Libera.Chat);
  `PLAIN` over TLS works everywhere.

## Interactive agent behaviours

### Observe mode / spontaneous contribution
With `observe_mode: true`, the bot keeps a rolling per-channel buffer of recent
lines (attached as `channel_context` so replies are conversation-aware) and may
*occasionally* contribute to unaddressed chatter. A reply fires only when
`random() < spontaneous_probability` **and** at least `spontaneous_cooldown`
seconds have passed since its last spontaneous post in that channel. The agent
is prompted that it may stay silent — replying with exactly `<silent>` is
suppressed (no message sent). Addressed messages (`require_mention`) always get
a reply. Env: `IRCX_OBSERVE_MODE`, `IRCX_SPONTANEOUS_PROBABILITY`,
`IRCX_SPONTANEOUS_COOLDOWN`, `IRCX_CONTEXT_BUFFER`.

### Runtime channel agency (agent tools)
When `allow_agent_join: true`, the agent gets four tools (toolset `ircx`):
`irc_join`, `irc_part`, `irc_say`, `irc_list_channels`. It can join/leave
channels and speak in any channel it has joined, on request. Joins are gated by
the `IRCX_JOINABLE_CHANNELS` allowlist (empty = any) and joined channels persist
across reconnects. `irc_say` only targets channels the bot is actually in (or a
nick for a DM). Disabled by default. Env: `IRCX_ALLOW_AGENT_JOIN`,
`IRCX_JOINABLE_CHANNELS`. Keep `IRCX_ALLOWED_USERS` tight when enabling this so
only trusted operators can direct joins.

### Downtime context persistence
IRC is stateless, but the **agent's own conversation memory persists** in
Hermes' state DB across reconnects/restarts. What's missed is channel traffic
*while the bot is offline*. Two mechanisms close that gap:
- **`draft/chathistory`** — on (re)join, IRCX requests `CHATHISTORY LATEST
  <chan> * <limit>` where the server supports it; replayed backlog is fed into
  the context buffer (and never re-answered). Env: `IRCX_CHATHISTORY_LIMIT`.
- **Logging mode** — set `IRCX_LOG_DIR` to append every channel line (with
  `server-time`) to a per-channel log; on (re)join the tail is replayed to seed
  the context buffer, so continuity survives even on servers without
  `chathistory`.
- For the strongest continuity, also run a **bouncer (ZNC / soju)** in front:
  point `IRCX_SERVER`/`IRCX_PORT` at the bouncer and it buffers + replays missed
  traffic 24/7 with no code changes.

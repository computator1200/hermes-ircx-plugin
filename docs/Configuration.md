# Configuration

IRCX reads configuration from **environment variables** (in `~/.hermes/.env`) and/or **`config.yaml`** under `gateway.platforms.ircx.extra`. **Precedence:** env (`IRCX_*`, then legacy `IRC_*`) overrides `config.yaml`.

Every `IRCX_*` variable falls back to its `IRC_*` equivalent, so configs written for the bundled `irc` example keep working.

## Connection

| Env | config.yaml `extra` | Default | Notes |
|---|---|---|---|
| `IRCX_SERVER` | `server` / `host` | — | **required** |
| `IRCX_PORT` | `port` | 6697 (TLS) / 6667 | |
| `IRCX_USE_TLS` | `use_tls` / `tls` | `true` | |
| `IRCX_TLS_VERIFY` | `tls_verify` | `true` | set `false` only for self-signed test servers |
| `IRCX_TLS_CLIENT_CERT` | `tls_client_cert` | — | PEM, for SASL `EXTERNAL` / CertFP |
| `IRCX_TLS_CLIENT_KEY` | `tls_client_key` | = cert | |
| `IRCX_NICKNAME` | `nickname` | `hermes-bot` | |
| `IRCX_USERNAME` | `username` | = nick | ident |
| `IRCX_REALNAME` | `realname` | `Hermes Agent` | |
| `IRCX_SERVER_PASSWORD` | `server_password` | — | `PASS` command |

## Channels

| Env | config.yaml `extra` | Notes |
|---|---|---|
| `IRCX_CHANNEL` / `IRCX_CHANNELS` | `channels` / `channel` | comma-separated; inline key allowed: `#ops secret` |
| `IRCX_HOME_CHANNEL` | `home_channel` | cron/notification target; defaults to first channel |

`config.yaml` supports rich channel entries and per-channel **groups**:

```yaml
gateway:
  platforms:
    ircx:
      enabled: true
      extra:
        server: irc.libera.chat
        channels:
          - "#hermes"
          - { name: "#ops", key: "s3cret" }
        groups:
          "#ops":
            require_mention: false           # respond to all messages here
            allow_from: ["alice", "bob"]      # channel-specific allowlist
            tools: ["web", "memory"]          # toolset scope (needs core patch)
            tools_by_sender:
              alice: ["*"]                    # per-sender override
```

## Authentication

See **[Authentication and SASL](Authentication-and-SASL.md)** for the full model.

| Env | Notes |
|---|---|
| `IRCX_SASL_MECHANISM` | `PLAIN` / `EXTERNAL` / `SCRAM-SHA-256` / `SCRAM-SHA-512` |
| `IRCX_SASL_USERNAME` | defaults to nick |
| `IRCX_SASL_PASSWORD` | for PLAIN / SCRAM |
| `IRCX_NICKSERV_PASSWORD` | fallback if SASL not configured |
| `IRCX_NICKSERV_SERVICE` | default `NickServ` |
| `IRCX_ALLOWED_USERS` | comma-separated verified accounts (or nicks) allowed to command the bot |
| `IRCX_ALLOW_ALL_USERS` | `true` = anyone (dev only) |
| `IRCX_DANGEROUSLY_ALLOW_NAME_MATCHING` | authorize by bare nick instead of verified account (insecure) |

## Behaviour & limits

| Env | Default | Notes |
|---|---|---|
| `IRCX_REQUIRE_MENTION` | `true` | only respond when addressed in channels |
| `IRCX_GROUP_POLICY` | `allowlist` | `allowlist` (configured channels only) or `open` |
| `IRCX_MAX_MESSAGE_LENGTH` | 450 | bytes of content per line before splitting |
| `IRCX_OBSERVE_MODE` | `false` | see [Interactive Behaviours](Interactive-Behaviours.md) |
| `IRCX_SPONTANEOUS_PROBABILITY` | 0 | 0..1 chance to chime in unprompted |
| `IRCX_SPONTANEOUS_COOLDOWN` | 90 | seconds between spontaneous posts/channel |
| `IRCX_CONTEXT_BUFFER` | 15 | recent lines kept per channel for context |
| `IRCX_ALLOW_AGENT_JOIN` | `false` | enable `irc_join`/`irc_part` tools |
| `IRCX_JOINABLE_CHANNELS` | — | allowlist for agent joins (empty = any) |
| `IRCX_LOG_DIR` | — | enable channel logging + tail replay |
| `IRCX_CHATHISTORY_LIMIT` | 50 | `draft/chathistory` backfill size on (re)join |

`config.yaml`-only extras: `mention_aliases` (extra names that count as addressing), `convert_formatting` (`true` → markdown becomes mIRC bold/italic codes instead of being stripped), `rate_limit: {burst, per_second}`.

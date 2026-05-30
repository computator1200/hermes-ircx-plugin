# IRCX Wiki

A full **IRCv3** platform adapter for [Hermes Agent](https://github.com/NousResearch/hermes-agent), built on `irctokens` + `ircstates`.

> Repo: **[computator1200/hermes-ircx-plugin](https://github.com/computator1200/hermes-ircx-plugin)** · Platform name: `ircx`

## 📚 Pages

- **[Configuration](Configuration.md)** — every `IRCX_*` env var, `config.yaml` schema, per-channel `groups`, precedence.
- **[Interactive Behaviours](Interactive-Behaviours.md)** — observe-mode spontaneous replies, runtime channel-agency tools, downtime context persistence.
- **[Authentication and SASL](Authentication-and-SASL.md)** — the verified-account identity model, SASL mechanisms, allowlists, hardening.
- **[Troubleshooting](Troubleshooting.md)** — connect / join / reply / reconnect issues and fixes.

## 60-second start

```bash
# 1. dependencies (into the env your Hermes gateway runs from)
<hermes-venv>/bin/python -m pip install irctokens ircstates

# 2. install the plugin
cp -r plugins/platforms/ircx ~/.hermes/plugins/ircx

# 3. minimal config (~/.hermes/.env)
IRCX_SERVER=irc.libera.chat
IRCX_CHANNEL=#yourchannel
IRCX_NICKNAME=hermes-bot
IRCX_SASL_MECHANISM=PLAIN
IRCX_SASL_USERNAME=hermes
IRCX_SASL_PASSWORD=...
IRCX_ALLOWED_USERS=youraccount
```

Restart the gateway. In a channel, address the bot by nick (`hermes-bot: hello`) and it replies.

## What you get

| Area | What you get |
|---|---|
| Protocol | Full IRCv3 CAP negotiation; only ACKed caps used |
| Auth | SASL PLAIN / EXTERNAL / SCRAM-256 / SCRAM-512; verified-account authorization |
| Resilience | Flood protection, keepalive + ping-timeout, gateway-driven reconnect (rejoin + re-auth) |
| Context | `draft/chathistory` backfill + on-disk logging tail-replay across disconnects |
| Agency | `irc_join` / `irc_part` / `irc_say` / `irc_list_channels` tools, opt-in + allowlisted |
| Presence | Observe-mode spontaneous contribution with probability + cooldown |

Not affiliated with or endorsed by Nous Research.

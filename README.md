# hermes-ircx-plugin

A **production IRCv3 platform adapter** for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — connect your Hermes agent to IRC channels and DMs with full IRCv3 support.

Built on the [`irctokens`](https://pypi.org/project/irctokens/) + [`ircstates`](https://pypi.org/project/ircstates/) stack (the de-facto correct, sans-IO IRCv3 parsing/state libraries). This is a feature superset of the stdlib `irc` example that ships with Hermes, and reaches parity with (and beyond) the OpenClaw IRC channel.

> Platform name: **`ircx`**. It coexists with the bundled `irc` example, so you can run both side-by-side and switch over when ready.

## Features

- **IRCv3 capability negotiation** (`CAP LS 302` → `REQ` → `END`), only using ACKed caps.
- **SASL**: `PLAIN`, `EXTERNAL` (CertFP), `SCRAM-SHA-256`, `SCRAM-SHA-512`. NickServ `IDENTIFY` fallback.
- **Verified-account authorization** via `account-tag` / `extended-join` — authorize by the network-verified account, not the spoofable nick (bare-nick auth is opt-in).
- **Multi-channel**, channel keys, server password, ISUPPORT-aware casemapping + message splitting, CTCP, flood protection, keepalive + ping-timeout, and gateway-driven reconnect (rejoins + re-auths).
- **Message tags**: `server-time`, `msgid`, `+draft/reply` threaded replies, `+typing` notifications.
- **OpenClaw parity**: `group_policy`, per-channel `groups` (`require_mention` / `allow_from` / `tools` / `tools_by_sender`), DM allowlists.
- **Interactive agent behaviours**:
  - *Observe mode* — keeps rolling channel context and can spontaneously contribute (probability + cooldown gated); declines with `<silent>`.
  - *Runtime channel agency* — `irc_join` / `irc_part` / `irc_say` / `irc_list_channels` tools, gated behind `IRCX_ALLOW_AGENT_JOIN` + a `joinable_channels` allowlist.
  - *Downtime context persistence* — IRCv3 `draft/chathistory` backfill on (re)join, plus optional on-disk channel logging with tail-replay (and works great behind a ZNC/soju bouncer).

See [`plugins/platforms/ircx/README.md`](plugins/platforms/ircx/README.md) for the full feature + behaviour reference, and [`plugins/platforms/ircx/plugin.yaml`](plugins/platforms/ircx/plugin.yaml) for every config/env var.

## Install

**1. Dependencies** — install into the environment your Hermes gateway runs from:

```bash
<hermes-venv>/bin/python -m pip install irctokens ircstates
```

**2. Drop in the plugin** — two equivalent options:

```bash
# (a) As a user plugin
cp -r plugins/platforms/ircx ~/.hermes/plugins/ircx

# (b) As a bundled platform (inside a hermes-agent checkout)
cp -r plugins/platforms/ircx <hermes-agent>/plugins/platforms/ircx
```

**3. Configure** (env vars in `~/.hermes/.env`, or `config.yaml` under `gateway.platforms.ircx.extra`):

```bash
IRCX_SERVER=irc.libera.chat
IRCX_CHANNEL=#yourchannel
IRCX_NICKNAME=hermes-bot
IRCX_SASL_MECHANISM=PLAIN
IRCX_SASL_USERNAME=...
IRCX_SASL_PASSWORD=...
IRCX_ALLOWED_USERS=youraccount          # verified accounts allowed to command the bot
```

Then restart the gateway. Every `IRCX_*` variable also falls back to the legacy `IRC_*` name, so existing example-plugin setups work unchanged.

## Optional: per-channel tool scoping (requires a small core patch)

`groups.<chan>.tools` / `tools_by_sender` (toolset-level scoping) is enforced via a tiny, generic, backward-compatible core hook documented in [`CORE_PATCH.md`](CORE_PATCH.md). Without that patch the plugin still works — the scope is simply ignored. The patch is a good candidate to upstream to Hermes.

## Tests

`tests/` are written against the Hermes test harness. Run them from inside a `hermes-agent` checkout that has this plugin installed at `plugins/platforms/ircx/`:

```bash
cp tests/test_ircx_*.py <hermes-agent>/tests/gateway/
cd <hermes-agent>
./venv/bin/python -m pytest tests/gateway/test_ircx_adapter.py tests/gateway/test_ircx_features.py -q
```

91 network-free tests cover config, message splitting, the CAP/SASL state machine (incl. SCRAM-256/512 vs a reference server), mention gating, the verified-account authorization matrix, CTCP, observe-mode, the agency tools, and chathistory/logging. The full connect → CAP → registration → JOIN path has also been validated live against Libera.Chat and Rizon.

## Status

Working and tested, but young — please file issues / PRs. Not affiliated with or endorsed by Nous Research.

## License

MIT — see [LICENSE](LICENSE). Built with the Hermes Agent platform-adapter SDK.

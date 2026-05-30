# Troubleshooting

Logs live in your profile's `logs/` dir (`gateway.log`, `agent.log`). Set `IRCX_LOG_DIR` to also capture raw channel lines.

## Plugin doesn't load / platform missing

- **`irctokens` / `ircstates` not installed** — the plugin disables itself with an install hint. Install into the *same* env your gateway runs from:
  `<hermes-venv>/bin/python -m pip install irctokens ircstates`
- Confirm it's discovered: `ircx` should appear in `hermes status` / the platform registry once `IRCX_SERVER` + `IRCX_CHANNEL` are set.

## Connects but never finishes registering

- Symptom: `IRC registration timed out (no RPL_WELCOME)`.
- Usually a **bad `USER`/identity** or the server is waiting on CAP. IRCX sends `CAP END` after negotiation; if you patched the code, ensure the `USER` line has a non-empty username (it falls back to the nick).
- Check TLS/port: TLS default is 6697; plaintext is 6667 with `IRCX_USE_TLS=false`.

## Can't connect at all

- `connect_failed` → wrong host/port, firewall, or TLS verification failure.
- Self-signed test server: `IRCX_TLS_VERIFY=false` (test only).
- Outbound 6697 blocked on your host? Try a bouncer or a different port the network offers.

## Joins the network but not the channel

- **Channel key** required → use `#chan key` (inline) or the `key` field.
- `+n` (no external messages) / `+i` (invite-only) / `+k` (keyed) → the bot must actually JOIN before speaking; keyed/invite channels need the key/invite.
- Channel name typo or `IRCX_GROUP_POLICY=allowlist` excluding it.

## Doesn't respond to messages

- In channels, `require_mention` is **on** by default — address it by nick (`botnick: hi`). Add alternate names via `mention_aliases`.
- **Authorization**: by default only **verified accounts** are allowed. If you're testing, set `IRCX_ALLOW_ALL_USERS=true`, or add your account to `IRCX_ALLOWED_USERS`. See [Authentication and SASL](Authentication-and-SASL.md).
- Via a **Matrix/heisenbridge** puppet, your IRC-side nick is the bridge puppet (e.g. `yourname`), not your Matrix handle — that's the identity the bot sees.

## SASL fails

- `SASL failed (904/905)` → wrong account/password, or the mechanism isn't offered. Check the server's advertised `sasl=` list (in the `CAP LS`).
- **Mechanism mismatch**: Libera.Chat offers `SCRAM-SHA-512` (not 256) and `EXTERNAL`/`PLAIN`. Pick one the server advertises.

## Reconnect behaviour

- On a dropped link or **ping timeout**, IRCX marks a retryable failure; Hermes' background reconnect watcher re-runs `connect()`, which re-auths and **rejoins all channels** (including ones added at runtime via `irc_join`).
- The agent's conversation memory is **not** lost on reconnect — it's persisted in Hermes' state DB. Only messages sent *while offline* are missed; see persistence options in [Interactive Behaviours](Interactive-Behaviours.md).

## Spontaneous replies too frequent / never happen

- Too chatty → lower `IRCX_SPONTANEOUS_PROBABILITY` and/or raise `IRCX_SPONTANEOUS_COOLDOWN`.
- Never fires → ensure `IRCX_OBSERVE_MODE=true` and probability `> 0`; low-traffic channels naturally fire rarely.

## `irc_join` / agency tools "disabled"

- Set `IRCX_ALLOW_AGENT_JOIN=true`, ensure the target is within `IRCX_JOINABLE_CHANNELS` (or leave it empty for any), and make sure the `ircx` toolset is enabled for the platform (`platform_toolsets.ircx`).

## Per-channel tool scoping has no effect

- `groups.<chan>.tools` / `tools_by_sender` enforcement needs the small core hook in **CORE_PATCH.md**. Without it the plugin runs fine but the scope is ignored.

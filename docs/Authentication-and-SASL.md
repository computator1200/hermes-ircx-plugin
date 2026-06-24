# Authentication & SASL

IRCX separates **two** questions: *who is this user (identity)* and *are they allowed to command the bot (authorization)*.

## Identity: verified account, not nick

IRC nicks are **not** authenticated - anyone can grab any nick. IRCX therefore identifies a sender by their **network-verified account** (learned via the IRCv3 `account-tag` / `extended-join` / `account-notify` capabilities, typically backed by NickServ/SASL).

- By default (`dangerously_allow_name_matching: false`), a user with **no known account is not authorized** - even if their nick is on your allowlist.
- The verified account is what's matched against allowlists and fed to Hermes' central authorization as `user_id`.
- Set `IRCX_DANGEROUSLY_ALLOW_NAME_MATCHING=true` only on trusted/private networks - it authorizes by bare nick and is spoofable.

This mirrors OpenClaw's `dangerouslyAllowNameMatching`.

## SASL mechanisms

Configure via `IRCX_SASL_MECHANISM` (+ username/password). IRCX negotiates SASL inside CAP and only completes `CAP END` after the SASL exchange resolves.

| Mechanism | Use it when |
|---|---|
| `PLAIN` | Most common. Account + password over TLS. |
| `EXTERNAL` | CertFP - set `IRCX_TLS_CLIENT_CERT` (+ key); no password sent. |
| `SCRAM-SHA-256` | Networks offering SCRAM-256 (challenge-response; password never sent). |
| `SCRAM-SHA-512` | e.g. **Libera.Chat** offers SCRAM-SHA-512. |

If SASL isn't configured, IRCX falls back to **NickServ** `IDENTIFY` (`IRCX_NICKSERV_PASSWORD`) after registration - but SASL is strongly preferred (authenticates *before* joining).

## Authorization (who may command the bot)

Checked in this order; first decisive rule wins:

1. `IRCX_ALLOW_ALL_USERS=true` → everyone (dev only).
2. **Channel** message: per-channel `groups.<chan>.allow_from`, else global `group_allow_from`.
3. **DM**: `allow_from`.
4. Otherwise `IRCX_ALLOWED_USERS` (the adapter-side list) - and Hermes' central `_is_user_authorized` (pairing / global allow-all) as the final gate.

`IRCX_GROUP_POLICY=allowlist` (default) means the bot only engages in channels it's configured for; `open` lets it engage anywhere it's present.

## Hardening checklist

- ✅ TLS on (`IRCX_USE_TLS=true`, the default); keep `IRCX_TLS_VERIFY=true`.
- ✅ Prefer **SASL** over NickServ; prefer SCRAM/EXTERNAL over PLAIN where available.
- ✅ Keep `dangerously_allow_name_matching` **off** on public networks.
- ✅ Scope `IRCX_ALLOWED_USERS` to specific verified accounts; avoid `IRCX_ALLOW_ALL_USERS` outside testing.
- ✅ If you enable **agent-join**, keep the allowlist of commanders tight and set `IRCX_JOINABLE_CHANNELS`.
- ✅ All outbound content and targets are stripped of CR/LF/NUL to prevent IRC command injection.

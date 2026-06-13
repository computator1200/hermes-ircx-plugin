"""
IRCX — IRCv3 Platform Adapter for Hermes Agent
=========================================================

A gateway platform adapter that connects Hermes to one or more IRC channels
(and DMs) with full IRCv3 support, built on the ``irctokens`` + ``ircstates``
stack.

Highlights
----------
* **IRCv3 capability negotiation** (``CAP LS 302`` → ``REQ`` → ``END``) with a
  configurable desired-cap set; only ACKed caps are used.
* **SASL** authentication: ``PLAIN``, ``EXTERNAL`` (CertFP) and
  ``SCRAM-SHA-256``.  Falls back to NickServ ``IDENTIFY`` when SASL is not
  configured.
* **Verified-account authorization** via ``account-tag`` / ``extended-join`` /
  ``account-notify`` — authorize by the network-verified account, not the
  spoofable nick.  Bare-nick matching is opt-in
  (``dangerously_allow_name_matching``), mirroring OpenClaw's
  ``dangerouslyAllowNameMatching``.
* **Feature parity with the OpenClaw IRC channel**: ``groupPolicy`` /
  ``groups`` / ``allowFrom`` / ``requireMention`` / per-channel + per-sender
  tool scoping, multi-channel, channel keys, NickServ, server password.
* **ISUPPORT-aware**: casemapping for nick/channel comparison, ``CHANTYPES``
  for channel detection, ``LINELEN`` for splitting.
* **Robustness**: outbound flood protection (token bucket), keepalive with
  ping-timeout detection, graceful retryable-failure signalling so the
  gateway's background reconnect watcher re-establishes the link and rejoins.
* **CTCP**: replies to ``VERSION``/``PING``/``TIME``/``CLIENTINFO``/``SOURCE``
  and renders ``ACTION`` (``/me``) inbound.
* **Typing notifications** (IRCv3 ``+typing`` client tag) and **threaded
  replies** (``+draft/reply``) when the server supports message tags.

Configuration (``config.yaml``)::

    gateway:
      platforms:
        ircx:
          enabled: true
          extra:
            server: irc.libera.chat
            port: 6697
            use_tls: true
            nickname: hermes-bot
            username: hermes
            realname: Hermes Agent
            channels:
              - "#hermes"
              - { name: "#ops", key: "s3cret" }
            sasl: { mechanism: PLAIN, username: hermes, password: "..." }
            nickserv: { password: "..." }          # alternative to SASL
            require_mention: true
            group_policy: allowlist                 # or "open"
            dangerously_allow_name_matching: false
            allow_from: ["alice", "bob"]            # DM allowlist (accounts)
            group_allow_from: ["alice"]             # global channel allowlist
            groups:
              "#ops":
                require_mention: false
                allow_from: ["alice"]
                tools: ["read_file", "web_search"]  # scope tools in this channel
                tools_by_sender:
                  alice: ["*"]
            max_message_length: 450
            rate_limit: { burst: 5, per_second: 2 }

Or via environment variables (``IRCX_*``, falling back to the bundled
example's ``IRC_*`` names).  Env values override ``config.yaml``.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import hmac
import logging
import os
import re
import secrets
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IRCv3 library stack (optional at import time so the module can still be
# imported — and ``check_requirements`` can report a helpful install hint —
# when the packages are not installed).
# ---------------------------------------------------------------------------
try:
    import irctokens  # type: ignore
    import ircstates  # type: ignore
    from ircstates import numerics as _NUM  # type: ignore

    _LIBS_OK = True
    _LIBS_ERR = ""
except Exception as _e:  # pragma: no cover - exercised only without deps
    irctokens = None  # type: ignore
    ircstates = None  # type: ignore
    _NUM = None  # type: ignore
    _LIBS_OK = False
    _LIBS_ERR = str(_e)

# Hermes gateway SDK.
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.config import Platform, PlatformConfig  # noqa: F401  (Platform used)

INSTALL_HINT = "pip install irctokens ircstates"

# IRCv3 capabilities we request when the server offers them.  We only ever
# *use* the ones the server ACKs, so listing extras here is safe.
DESIRED_CAPS = (
    "sasl",
    "message-tags",
    "server-time",
    "account-tag",
    "account-notify",
    "extended-join",
    "away-notify",
    "chghost",
    "multi-prefix",
    "userhost-in-names",
    "cap-notify",
    "echo-message",
    "setname",
    "batch",
    "labeled-response",
    "draft/message-redaction",
    "draft/chathistory",
)

_CTCP_VERSION = "Hermes Agent IRCX (irctokens/ircstates)"


# ===========================================================================
# Configuration
# ===========================================================================

def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env(*names: str) -> Optional[str]:
    """Return the first set, non-empty environment variable among *names*."""
    for name in names:
        val = os.getenv(name)
        if val is not None and val.strip() != "":
            return val.strip()
    return None


@dataclass
class ChannelSpec:
    """A channel to join, with optional key and per-channel overrides."""
    name: str
    key: Optional[str] = None
    require_mention: Optional[bool] = None  # None = inherit global
    allow_from: Optional[List[str]] = None  # None = inherit group_allow_from
    tools: Optional[List[str]] = None
    tools_by_sender: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class IRCXConfig:
    """Fully-resolved IRCX configuration (env > config.yaml)."""
    server: str = ""
    port: int = 6697
    use_tls: bool = True
    tls_verify: bool = True
    tls_client_cert: Optional[str] = None
    tls_client_key: Optional[str] = None

    nickname: str = "hermes-bot"
    username: str = ""
    realname: str = "Hermes Agent"
    server_password: Optional[str] = None

    sasl_mechanism: Optional[str] = None  # PLAIN | EXTERNAL | SCRAM-SHA-256
    sasl_username: Optional[str] = None
    sasl_password: Optional[str] = None

    nickserv_password: Optional[str] = None
    nickserv_service: str = "NickServ"

    channels: List[ChannelSpec] = field(default_factory=list)

    require_mention: bool = True
    group_policy: str = "allowlist"  # allowlist | open
    dangerously_allow_name_matching: bool = False

    allow_from: List[str] = field(default_factory=list)        # DM allowlist
    group_allow_from: List[str] = field(default_factory=list)  # channel allowlist

    max_message_length: int = 450
    rate_burst: int = 5
    rate_per_second: float = 2.0
    mention_aliases: List[str] = field(default_factory=list)

    convert_formatting: bool = False  # markdown -> mIRC control codes
    home_channel: Optional[str] = None
    ping_interval: float = 120.0
    ping_timeout: float = 60.0

    # --- Observe / spontaneous contribution (Feature A) ---
    observe_mode: bool = False           # process unaddressed channel chatter
    spontaneous_probability: float = 0.0  # 0..1 chance to chime in unprompted
    spontaneous_cooldown: float = 90.0    # min seconds between spontaneous posts/chan
    context_buffer_size: int = 15         # recent lines kept per channel for context
    show_events: bool = False             # surface join/part/quit/kick/nick to context

    # --- Runtime agency tools (Feature B) ---
    allow_agent_join: bool = False        # let the agent JOIN/PART at runtime
    joinable_channels: List[str] = field(default_factory=list)  # empty = any
    blocked_channels: List[str] = field(default_factory=list)   # denylist; always wins
    allow_agent_kick: bool = False        # let the agent KICK (still needs to be a channel op)

    # --- Context persistence across disconnects (Feature C) ---
    log_dir: Optional[str] = None         # if set, log channel lines + replay tail
    chathistory_limit: int = 50           # CHATHISTORY LATEST fetch size on (re)join

    # ---- derived helpers -------------------------------------------------

    def channel_names(self) -> List[str]:
        return [c.name for c in self.channels]

    def channel_spec(self, name_cf: str, casefold: Callable[[str], str]) -> Optional[ChannelSpec]:
        for c in self.channels:
            if casefold(c.name) == name_cf:
                return c
        return None


def _parse_channels(raw: Any) -> List[ChannelSpec]:
    """Parse channels from a list (str/dict) or a comma-separated string.

    String form supports an inline key: ``"#ops secret"``.
    """
    specs: List[ChannelSpec] = []
    if raw is None:
        return specs
    items: List[Any]
    if isinstance(raw, str):
        items = [p.strip() for p in raw.split(",") if p.strip()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return specs

    for item in items:
        if isinstance(item, str):
            parts = item.split(None, 1)
            name = parts[0]
            key = parts[1].strip() if len(parts) > 1 else None
            specs.append(ChannelSpec(name=name, key=key))
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("channel") or "").strip()
            if not name:
                continue
            specs.append(
                ChannelSpec(
                    name=name,
                    key=item.get("key"),
                    require_mention=item.get("require_mention"),
                    allow_from=item.get("allow_from"),
                    tools=item.get("tools"),
                    tools_by_sender=item.get("tools_by_sender", {}) or {},
                )
            )
    return specs


def _coerce_str_list(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(",") if p.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(p).strip() for p in raw if str(p).strip()]
    return []


def load_config(platform_config: Any) -> IRCXConfig:
    """Build an :class:`IRCXConfig` from a ``PlatformConfig`` + environment.

    Precedence: environment (``IRCX_*`` then legacy ``IRC_*``) overrides
    ``config.yaml`` ``extra`` keys.
    """
    extra: Dict[str, Any] = getattr(platform_config, "extra", {}) or {}
    cfg = IRCXConfig()

    cfg.server = _env("IRCX_SERVER", "IRC_SERVER") or str(extra.get("server", extra.get("host", "")))

    port_raw = _env("IRCX_PORT", "IRC_PORT") or extra.get("port")
    use_tls_env = _env("IRCX_USE_TLS", "IRC_USE_TLS", "IRC_TLS")
    if use_tls_env is not None:
        cfg.use_tls = _truthy(use_tls_env)
    else:
        cfg.use_tls = bool(extra.get("use_tls", extra.get("tls", True)))
    try:
        cfg.port = int(port_raw) if port_raw is not None else (6697 if cfg.use_tls else 6667)
    except (TypeError, ValueError):
        cfg.port = 6697 if cfg.use_tls else 6667

    tls_verify_env = _env("IRCX_TLS_VERIFY")
    cfg.tls_verify = _truthy(tls_verify_env) if tls_verify_env is not None else bool(extra.get("tls_verify", True))
    cfg.tls_client_cert = _env("IRCX_TLS_CLIENT_CERT") or extra.get("tls_client_cert")
    cfg.tls_client_key = _env("IRCX_TLS_CLIENT_KEY") or extra.get("tls_client_key")

    cfg.nickname = _env("IRCX_NICKNAME", "IRC_NICKNAME") or str(extra.get("nickname", "hermes-bot"))
    cfg.username = _env("IRCX_USERNAME", "IRC_USERNAME") or str(extra.get("username", "")) or cfg.nickname
    cfg.realname = _env("IRCX_REALNAME", "IRC_REALNAME") or str(extra.get("realname", "Hermes Agent"))
    cfg.server_password = _env(
        "IRCX_SERVER_PASSWORD", "IRC_SERVER_PASSWORD", "IRC_PASSWORD"
    ) or extra.get("server_password")

    # SASL (env or extra["sasl"] dict)
    sasl_extra = extra.get("sasl") if isinstance(extra.get("sasl"), dict) else {}
    cfg.sasl_mechanism = _env("IRCX_SASL_MECHANISM") or sasl_extra.get("mechanism")
    if cfg.sasl_mechanism:
        cfg.sasl_mechanism = cfg.sasl_mechanism.strip().upper()
    cfg.sasl_username = _env("IRCX_SASL_USERNAME") or sasl_extra.get("username") or cfg.nickname
    cfg.sasl_password = _env("IRCX_SASL_PASSWORD") or sasl_extra.get("password")

    # NickServ (env or extra["nickserv"] dict)
    ns_extra = extra.get("nickserv") if isinstance(extra.get("nickserv"), dict) else {}
    cfg.nickserv_password = _env(
        "IRCX_NICKSERV_PASSWORD", "IRC_NICKSERV_PASSWORD"
    ) or ns_extra.get("password")
    cfg.nickserv_service = _env("IRCX_NICKSERV_SERVICE") or ns_extra.get("service", "NickServ")

    # Channels
    chans = _env("IRCX_CHANNEL", "IRCX_CHANNELS", "IRC_CHANNEL", "IRC_CHANNELS")
    if chans is not None:
        cfg.channels = _parse_channels(chans)
    else:
        cfg.channels = _parse_channels(extra.get("channels") or extra.get("channel"))

    # Per-channel overrides from extra["groups"] (OpenClaw-style)
    groups = extra.get("groups") if isinstance(extra.get("groups"), dict) else {}
    if groups:
        existing = {c.name.lower(): c for c in cfg.channels}
        for chan_name, rule in groups.items():
            if not isinstance(rule, dict):
                continue
            spec = existing.get(str(chan_name).lower())
            if spec is None:
                spec = ChannelSpec(name=str(chan_name))
                cfg.channels.append(spec)
                existing[str(chan_name).lower()] = spec
            if "require_mention" in rule:
                spec.require_mention = _truthy(rule["require_mention"])
            if "allow_from" in rule:
                spec.allow_from = _coerce_str_list(rule["allow_from"])
            if "tools" in rule:
                spec.tools = _coerce_str_list(rule["tools"])
            if "tools_by_sender" in rule and isinstance(rule["tools_by_sender"], dict):
                spec.tools_by_sender = {
                    str(k): _coerce_str_list(v) for k, v in rule["tools_by_sender"].items()
                }

    rm_env = _env("IRCX_REQUIRE_MENTION", "IRC_REQUIRE_MENTION")
    cfg.require_mention = _truthy(rm_env) if rm_env is not None else bool(extra.get("require_mention", True))

    cfg.group_policy = (_env("IRCX_GROUP_POLICY") or str(extra.get("group_policy", "allowlist"))).strip().lower()
    if cfg.group_policy not in {"allowlist", "open"}:
        cfg.group_policy = "allowlist"

    dnm_env = _env("IRCX_DANGEROUSLY_ALLOW_NAME_MATCHING")
    cfg.dangerously_allow_name_matching = (
        _truthy(dnm_env) if dnm_env is not None
        else bool(extra.get("dangerously_allow_name_matching", False))
    )

    cfg.allow_from = _coerce_str_list(_env("IRCX_ALLOW_FROM") or extra.get("allow_from"))
    cfg.group_allow_from = _coerce_str_list(_env("IRCX_GROUP_ALLOW_FROM") or extra.get("group_allow_from"))
    cfg.mention_aliases = _coerce_str_list(extra.get("mention_aliases"))

    mml = _env("IRCX_MAX_MESSAGE_LENGTH") or extra.get("max_message_length")
    try:
        cfg.max_message_length = int(mml) if mml is not None else 450
    except (TypeError, ValueError):
        cfg.max_message_length = 450

    rl = extra.get("rate_limit") if isinstance(extra.get("rate_limit"), dict) else {}
    try:
        cfg.rate_burst = int(rl.get("burst", 5))
    except (TypeError, ValueError):
        cfg.rate_burst = 5
    try:
        cfg.rate_per_second = float(rl.get("per_second", 2.0))
    except (TypeError, ValueError):
        cfg.rate_per_second = 2.0

    cfg.convert_formatting = bool(extra.get("convert_formatting", False))
    cfg.home_channel = _env("IRCX_HOME_CHANNEL", "IRC_HOME_CHANNEL") or extra.get("home_channel")
    if not cfg.home_channel and cfg.channels:
        cfg.home_channel = cfg.channels[0].name

    # --- Feature A: observe / spontaneous ---
    obs_env = _env("IRCX_OBSERVE_MODE")
    cfg.observe_mode = _truthy(obs_env) if obs_env is not None else bool(extra.get("observe_mode", False))
    prob = _env("IRCX_SPONTANEOUS_PROBABILITY") or extra.get("spontaneous_probability")
    try:
        cfg.spontaneous_probability = max(0.0, min(1.0, float(prob))) if prob is not None else 0.0
    except (TypeError, ValueError):
        cfg.spontaneous_probability = 0.0
    cd = _env("IRCX_SPONTANEOUS_COOLDOWN") or extra.get("spontaneous_cooldown")
    try:
        cfg.spontaneous_cooldown = float(cd) if cd is not None else 90.0
    except (TypeError, ValueError):
        cfg.spontaneous_cooldown = 90.0
    cbs = _env("IRCX_CONTEXT_BUFFER") or extra.get("context_buffer_size")
    try:
        cfg.context_buffer_size = max(0, int(cbs)) if cbs is not None else 15
    except (TypeError, ValueError):
        cfg.context_buffer_size = 15
    ev_env = _env("IRCX_SHOW_EVENTS")
    cfg.show_events = _truthy(ev_env) if ev_env is not None else bool(extra.get("show_events", False))

    # --- Feature B: runtime agency ---
    aj_env = _env("IRCX_ALLOW_AGENT_JOIN")
    cfg.allow_agent_join = _truthy(aj_env) if aj_env is not None else bool(extra.get("allow_agent_join", False))
    cfg.joinable_channels = _coerce_str_list(_env("IRCX_JOINABLE_CHANNELS") or extra.get("joinable_channels"))
    cfg.blocked_channels = _coerce_str_list(_env("IRCX_BLOCKED_CHANNELS") or extra.get("blocked_channels"))
    # A blocked channel is never auto-joined, even if listed in IRCX_CHANNEL.
    if cfg.blocked_channels:
        _blk = {c.lower() for c in cfg.blocked_channels}
        cfg.channels = [c for c in cfg.channels if c.name.lower() not in _blk]
    ak_env = _env("IRCX_ALLOW_AGENT_KICK")
    cfg.allow_agent_kick = _truthy(ak_env) if ak_env is not None else bool(extra.get("allow_agent_kick", False))

    # --- Feature C: persistence ---
    cfg.log_dir = _env("IRCX_LOG_DIR") or extra.get("log_dir")
    chl = _env("IRCX_CHATHISTORY_LIMIT") or extra.get("chathistory_limit")
    try:
        cfg.chathistory_limit = max(1, int(chl)) if chl is not None else 50
    except (TypeError, ValueError):
        cfg.chathistory_limit = 50

    return cfg


# ===========================================================================
# Text formatting / IRC-safety helpers
# ===========================================================================

# mIRC control codes
_C_BOLD = "\x02"
_C_ITALIC = "\x1d"
_C_RESET = "\x0f"


def strip_irc_control_chars(text: str) -> str:
    """Strip CR/LF/NUL so user content can't inject IRC commands."""
    return text.replace("\r", " ").replace("\n", " ").replace("\x00", "")


def _write_log_line(path: str, line: str) -> None:
    """Append a single line to *path*, swallowing I/O errors.

    Module-level so it can run on an executor thread without holding a
    reference to the adapter. Best-effort: logging must never take down the
    connection, so any failure is dropped.
    """
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def _strip_code_fences(text: str) -> str:
    """Remove triple-backtick fence *markers* (with optional language tag),
    keeping the code content. Handles fences anywhere on a line, including a
    multi-line block's opening and closing fences. Inline (single-backtick)
    handling is done separately, per line.

    Note: we deliberately do NOT run the bold/italic substitutions with
    ``re.DOTALL``. On IRC each rendered line stands alone, and a greedy
    ``.+?`` across newlines would let an unterminated ``**`` swallow an entire
    paragraph. Keeping ``.`` newline-bounded means a stray marker degrades to
    a single literal char on one line — never a broken multi-line render.
    """
    return re.sub(r"```[\w+.\-/]*[ \t]*\n?", "", text)


def strip_markdown(text: str) -> str:
    """Convert common markdown to plain text for IRC."""
    text = _strip_code_fences(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)        # **bold**
    text = re.sub(r"__(.+?)__", r"\1", text)            # __bold__
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)  # *italic*
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)  # _italic_
    text = re.sub(r"`(.+?)`", r"\1", text)              # `code`
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\2", text)        # image -> url
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)     # link -> text (url)
    return text


def markdown_to_irc(text: str) -> str:
    """Convert a subset of markdown to mIRC formatting control codes."""
    text = _strip_code_fences(text)
    text = re.sub(r"\*\*(.+?)\*\*", _C_BOLD + r"\1" + _C_BOLD, text)
    text = re.sub(r"__(.+?)__", _C_BOLD + r"\1" + _C_BOLD, text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", _C_ITALIC + r"\1" + _C_ITALIC, text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\2", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    return text


def split_message(content: str, target: str, user_limit: int, *, convert_formatting: bool = False) -> List[str]:
    """Split *content* into IRC-safe lines.

    Honours the ~512-byte protocol limit (after ``PRIVMSG <target> :``
    overhead) and the configured ``user_limit`` (in bytes of content),
    splitting on UTF-8 character boundaries and preferring spaces.
    """
    content = markdown_to_irc(content) if convert_formatting else strip_markdown(content)
    overhead = len(f"PRIVMSG {target} :".encode("utf-8")) + 2  # + CRLF
    max_bytes = 510 - overhead
    limit = min(user_limit, max_bytes) if user_limit > 0 else max_bytes

    lines: List[str] = []
    for paragraph in content.split("\n"):
        paragraph = strip_irc_control_chars(paragraph).rstrip()
        if not paragraph:
            continue
        while paragraph:
            if len(paragraph.encode("utf-8")) <= limit:
                lines.append(paragraph)
                break
            low, high, best = 1, len(paragraph), 1
            while low <= high:
                mid = (low + high) // 2
                if len(paragraph[:mid].encode("utf-8")) <= limit:
                    best = mid
                    low = mid + 1
                else:
                    high = mid - 1
            split_at = best
            space = paragraph.rfind(" ", 0, split_at)
            if space > split_at // 3:
                split_at = space
            lines.append(paragraph[:split_at].rstrip())
            paragraph = paragraph[split_at:].lstrip()
    return lines


# ===========================================================================
# SASL
# ===========================================================================

class SASLError(Exception):
    pass


def sasl_plain_payload(authcid: str, password: str, authzid: str = "") -> str:
    raw = f"{authzid}\x00{authcid}\x00{password}".encode("utf-8")
    # RFC 4616 SASL PLAIN: each field must be < 256 *bytes*. Some strict
    # daemons reject the AUTHENTICATE pre-base64. Fail clearly here rather
    # than have the server abort the handshake with an opaque error.
    for name, field in (("authcid", authcid), ("password", password), ("authzid", authzid)):
        if len(field.encode("utf-8")) > 255:
            raise SASLError(f"SASL PLAIN {name} exceeds 255 bytes")
    return base64.b64encode(raw).decode("ascii")


def _chunk_authenticate(payload_b64: str) -> List[str]:
    """Split a base64 SASL payload into 400-byte AUTHENTICATE chunks.

    Per the IRCv3 SASL spec, a payload that is an exact multiple of 400
    bytes requires a trailing empty ``+`` chunk so the server knows the
    message is complete.
    """
    if payload_b64 == "":
        return ["+"]
    chunks: List[str] = []
    for i in range(0, len(payload_b64), 400):
        chunks.append(payload_b64[i:i + 400])
    if len(payload_b64) % 400 == 0:
        chunks.append("+")
    return chunks


class ScramClient:
    """Minimal RFC 5802 SCRAM client (no channel binding).

    Supports any SHA-family hash via ``hash_name`` (``"sha256"`` /
    ``"sha512"``), so it works on networks offering SCRAM-SHA-256 or
    SCRAM-SHA-512 (e.g. Libera.Chat offers the latter).
    """

    def __init__(self, username: str, password: str, hash_name: str = "sha256"):
        self.username = username
        self.password = password
        self._hash_name = hash_name
        self._cnonce = base64.b64encode(secrets.token_bytes(18)).decode("ascii")
        self._client_first_bare = f"n={self._saslprep(username)},r={self._cnonce}"
        self._auth_message = ""
        self._server_signature = b""

    @staticmethod
    def _saslprep(value: str) -> str:
        # Minimal SASLprep: escape '=' and ',' per SCRAM username rules.
        return value.replace("=", "=3D").replace(",", "=2C")

    def client_first(self) -> bytes:
        return f"n,,{self._client_first_bare}".encode("utf-8")

    def client_final(self, server_first: bytes) -> bytes:
        attrs = dict(
            part.split("=", 1) for part in server_first.decode("utf-8").split(",") if "=" in part
        )
        rnonce = attrs["r"]
        if not rnonce.startswith(self._cnonce):
            raise SASLError("SCRAM: server nonce does not extend client nonce")
        salt = base64.b64decode(attrs["s"])
        iterations = int(attrs["i"])
        h = self._hash_name

        salted = hashlib.pbkdf2_hmac(h, self.password.encode("utf-8"), salt, iterations)
        client_key = hmac.new(salted, b"Client Key", h).digest()
        stored_key = hashlib.new(h, client_key).digest()
        channel_binding = base64.b64encode(b"n,,").decode("ascii")
        client_final_no_proof = f"c={channel_binding},r={rnonce}"
        auth_message = f"{self._client_first_bare},{server_first.decode('utf-8')},{client_final_no_proof}"
        client_sig = hmac.new(stored_key, auth_message.encode("utf-8"), h).digest()
        proof = bytes(a ^ b for a, b in zip(client_key, client_sig))

        server_key = hmac.new(salted, b"Server Key", h).digest()
        self._server_signature = hmac.new(server_key, auth_message.encode("utf-8"), h).digest()

        proof_b64 = base64.b64encode(proof).decode("ascii")
        return f"{client_final_no_proof},p={proof_b64}".encode("utf-8")

    def verify_server_final(self, server_final: bytes) -> bool:
        attrs = dict(
            part.split("=", 1) for part in server_final.decode("utf-8").split(",") if "=" in part
        )
        verifier = base64.b64decode(attrs.get("v", ""))
        return hmac.compare_digest(verifier, self._server_signature)


# Backwards-compatible alias.
ScramSha256Client = ScramClient


def _scram_hash_for(mechanism: str) -> Optional[str]:
    """Map a SCRAM mechanism name to a hashlib algorithm name."""
    if not mechanism.upper().startswith("SCRAM-SHA-"):
        return None
    suffix = mechanism.upper().rsplit("-", 1)[-1]  # "256" / "512" / "512-256"
    return {"256": "sha256", "512": "sha512"}.get(suffix)


# ===========================================================================
# IRC protocol client engine
# ===========================================================================

class IRCClient:
    """Async IRCv3 client: connection, CAP/SASL negotiation, state, I/O.

    The adapter owns one of these.  Inbound PRIVMSG/NOTICE are delivered via
    the ``on_message`` async callback; connection loss is reported via
    ``on_disconnect``.
    """

    def __init__(
        self,
        cfg: IRCXConfig,
        *,
        on_message: Callable[[Dict[str, Any]], Awaitable[None]],
        on_disconnect: Optional[Callable[[str], Awaitable[None]]] = None,
        nick_suffix: str = "",
    ):
        if not _LIBS_OK:
            raise RuntimeError(f"irctokens/ircstates not available: {_LIBS_ERR}")
        self.cfg = cfg
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._nick_suffix = nick_suffix

        self.server = ircstates.Server(cfg.nickname)
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

        self._recv_task: Optional[asyncio.Task] = None
        self._send_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        # Bounded so a flood (or a long rate-limit stall while inbound spikes)
        # applies backpressure instead of growing the queue without limit.
        self._send_q: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue(maxsize=512)

        self._desired_nick = cfg.nickname + nick_suffix
        self._nick_attempt = 0

        self._cap_available: Dict[str, str] = {}
        self._cap_acked: set = set()
        self._cap_requested: set = set()
        self._cap_negotiating = False
        self._cap_ls_buffer: List[str] = []

        self._registered_evt = asyncio.Event()
        self._sasl_done_evt = asyncio.Event()
        self._sasl_ok = False
        self._sasl_error: Optional[str] = None
        self._scram: Optional[ScramSha256Client] = None

        self._closing = False
        self._last_rx = 0.0
        self._awaiting_pong = False
        # Event loop this client runs on, captured at connect() so tool handlers
        # in Hermes' ThreadPoolExecutor can schedule coroutines back onto it.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Batch refs that belong to a draft/chathistory replay (Feature C),
        # so replayed messages can be flagged is_history and not re-answered.
        self._chathistory_batches: set = set()
        self._away_message = None
        self._whois_pending = {}

    # ---- public properties ----------------------------------------------

    @property
    def current_nick(self) -> str:
        return self.server.nickname or self._desired_nick

    def casefold(self, value: str) -> str:
        return self.server.casefold(value)

    def casefold_equals(self, a: str, b: str) -> bool:
        return self.server.casefold_equals(a, b)

    def is_channel(self, target: str) -> bool:
        try:
            return self.server.is_channel(target)
        except Exception:
            return bool(target) and target[0] in "#&+!"

    def has_cap(self, cap: str) -> bool:
        return cap in self._cap_acked

    # ---- connection lifecycle --------------------------------------------

    def _make_ssl_context(self) -> Optional[ssl.SSLContext]:
        if not self.cfg.use_tls:
            return None
        ctx = ssl.create_default_context()
        if not self.cfg.tls_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        if self.cfg.tls_client_cert:
            ctx.load_cert_chain(
                self.cfg.tls_client_cert,
                self.cfg.tls_client_key or self.cfg.tls_client_cert,
            )
        return ctx

    async def connect(self, timeout: float = 30.0) -> None:
        """Open the connection and complete IRCv3 + registration handshake.

        Raises on failure.  Idempotent: builds fresh state each call so the
        gateway's reconnect watcher can re-invoke it.
        """
        self._reset_state()
        self._loop = asyncio.get_running_loop()
        ssl_ctx = self._make_ssl_context()
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.cfg.server, self.cfg.port, ssl=ssl_ctx),
            timeout=timeout,
        )
        self._last_rx = asyncio.get_running_loop().time()

        self._send_task = asyncio.create_task(self._send_loop(), name="ircx-send")
        self._recv_task = asyncio.create_task(self._recv_loop(), name="ircx-recv")

        # Begin capability negotiation, then registration.
        self._cap_negotiating = True
        self.send_line("CAP", ["LS", "302"])
        if self.cfg.server_password:
            self.send_line("PASS", [strip_irc_control_chars(self.cfg.server_password)])
        self.send_line("NICK", [self._desired_nick])
        # ``username`` may be empty when an IRCXConfig is built directly (the
        # load_config path falls back to nick).  Guard here too so a bare
        # config still produces a valid USER command (avoids 461).
        self.send_line("USER", [self.cfg.username or self.cfg.nickname, "0", "*", self.cfg.realname or "Hermes Agent"])

        try:
            await asyncio.wait_for(self._registered_evt.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("IRC registration timed out (no RPL_WELCOME)") from exc

        # NickServ IDENTIFY when SASL was not used.
        if self.cfg.nickserv_password and not self._sasl_ok:
            self.send_line(
                "PRIVMSG",
                [self.cfg.nickserv_service, f"IDENTIFY {strip_irc_control_chars(self.cfg.nickserv_password)}"],
            )
            await asyncio.sleep(1.5)

        await self.join_configured_channels()

        self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="ircx-ping")

    async def join_configured_channels(self) -> None:
        keyed = [c for c in self.cfg.channels if c.key]
        plain = [c for c in self.cfg.channels if not c.key]
        if plain:
            self.send_line("JOIN", [",".join(c.name for c in plain)])
        for c in keyed:
            self.send_line("JOIN", [c.name, c.key])

    async def disconnect(self, message: str = "Hermes Agent shutting down") -> None:
        self._closing = True
        if self._writer and not self._writer.is_closing():
            try:
                self.send_line("QUIT", [message])
                await asyncio.sleep(0.2)
            except Exception:
                pass
        for task in (self._keepalive_task, self._recv_task, self._send_task):
            if task and not task.done():
                task.cancel()
        for task in (self._keepalive_task, self._recv_task, self._send_task):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass  # expected: we cancelled it just above
                except Exception:
                    # A real crash in a loop (e.g. a parser bug) must not be
                    # silently masked during shutdown — log it so it's
                    # diagnosable instead of vanishing.
                    logger.exception("IRCX: task crashed during disconnect")
        if self._writer:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
        self._reader = None
        self._writer = None

    def _reset_state(self) -> None:
        self.server = ircstates.Server(self.cfg.nickname)
        self._registered_evt = asyncio.Event()
        self._sasl_done_evt = asyncio.Event()
        self._sasl_ok = False
        self._sasl_error = None
        self._scram = None
        self._cap_available.clear()
        self._cap_acked.clear()
        self._cap_requested.clear()
        self._cap_ls_buffer.clear()
        self._cap_negotiating = False
        self._closing = False
        self._nick_attempt = 0
        self._desired_nick = self.cfg.nickname + self._nick_suffix
        self._awaiting_pong = False
        # drain any stale queued lines
        while not self._send_q.empty():
            try:
                self._send_q.get_nowait()
            except asyncio.QueueEmpty:
                break

    # ---- raw I/O ----------------------------------------------------------

    def send_line(self, command: str, params: Optional[List[str]] = None, tags: Optional[Dict[str, str]] = None) -> None:
        """Queue an IRC line for sending (rate-limited by the send loop)."""
        line = irctokens.build(command, params or [], tags=tags)
        data = line.format().encode("utf-8") + b"\r\n"
        try:
            self._send_q.put_nowait(data)
        except asyncio.QueueFull:
            # Backpressure: outbound queue saturated (flood / long rate-limit
            # stall). Dropping the newest line beats unbounded memory growth;
            # PING/PONG keepalive still recovers a genuinely wedged link.
            logger.warning("IRCX: outbound queue full, dropping %s", command)

    async def _send_loop(self) -> None:
        """Drain the outbound queue with token-bucket flood protection."""
        loop = asyncio.get_running_loop()
        tokens = float(self.cfg.rate_burst)
        last = loop.time()
        refill = max(self.cfg.rate_per_second, 0.1)
        try:
            while True:
                data = await self._send_q.get()
                if data is None:
                    break
                now = loop.time()
                tokens = min(self.cfg.rate_burst, tokens + (now - last) * refill)
                last = now
                if tokens < 1.0:
                    await asyncio.sleep((1.0 - tokens) / refill)
                    tokens = 0.0
                else:
                    tokens -= 1.0
                if self._writer and not self._writer.is_closing():
                    self._writer.write(data)
                    await self._writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - network error path
            logger.debug("IRCX send loop ended: %s", exc)

    async def _recv_loop(self) -> None:
        try:
            while self._reader and not self._reader.at_eof():
                data = await self._reader.read(4096)
                if not data:
                    break
                self._last_rx = asyncio.get_running_loop().time()
                self._awaiting_pong = False
                for line in self.server.recv(data):
                    # QUIT/NICK remove or rename the user in parse_tokens below,
                    # so snapshot their shared channels first (for event context).
                    pre_channels = None
                    try:
                        if self.cfg.show_events and line.command.upper() in ("QUIT", "NICK"):
                            _src = line.hostmask.nickname if line.hostmask else (
                                line.source.split("!", 1)[0] if line.source else "")
                            pre_channels = self._user_shared_channels(_src)
                    except Exception:
                        pre_channels = None
                    try:
                        self.server.parse_tokens(line)
                    except Exception as exc:  # pragma: no cover
                        logger.debug("IRCX state parse error: %s", exc)
                    try:
                        await self._handle_line(line, pre_channels=pre_channels)
                    except Exception as exc:
                        logger.warning("IRCX line handler error on %s: %s", line.command, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover
            logger.warning("IRCX receive loop error: %s", exc)
        finally:
            if not self._closing:
                await self._fire_disconnect("connection closed")

    async def _keepalive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.cfg.ping_interval)
                loop = asyncio.get_running_loop()
                # Opportunistically reclaim our configured nick if we drifted
                # off it (mid-session 433 recovery, netsplit, forced change).
                if self._registered_evt.is_set():
                    self._maybe_regain_nick()
                idle = loop.time() - self._last_rx
                if idle < self.cfg.ping_interval:
                    continue
                if self._awaiting_pong and idle > (self.cfg.ping_interval + self.cfg.ping_timeout):
                    await self._fire_disconnect("ping timeout")
                    return
                self._awaiting_pong = True
                self.send_line("PING", [f"hermes{int(loop.time())}"])
        except asyncio.CancelledError:
            raise

    async def _fire_disconnect(self, reason: str) -> None:
        if self._closing:
            return
        self._closing = True
        if self._on_disconnect:
            try:
                await self._on_disconnect(reason)
            except Exception:
                pass

    # ---- protocol dispatch -----------------------------------------------

    async def _handle_line(self, line: Any, pre_channels: Optional[List[str]] = None) -> None:
        command = line.command.upper()

        if command == "PING":
            self.send_line("PONG", line.params)
            return
        if command == "PONG":
            self._awaiting_pong = False
            return
        if command == "CAP":
            await self._handle_cap(line)
            return
        if command == "AUTHENTICATE":
            await self._handle_authenticate(line)
            return

        if command == _NUM.RPL_WELCOME:  # 001
            if line.params:
                # ircstates already tracks nickname; keep desired in sync.
                self._desired_nick = line.params[0]
            self._registered_evt.set()
            return

        if command in (_NUM.ERR_NICKNAMEINUSE, "433", "436", "437"):
            await self._handle_nick_in_use()
            return

        # SASL result numerics
        if command in ("900",):  # RPL_LOGGEDIN
            return
        if command == "903":  # RPL_SASLSUCCESS
            self._sasl_ok = True
            self._finish_sasl()
            return
        if command in ("902", "904", "905", "906", "907", "908"):
            self._sasl_error = f"SASL failed ({command})"
            self._sasl_ok = False
            self._finish_sasl()
            return

        if command in ("PRIVMSG", "NOTICE"):
            await self._handle_privmsg(line, is_notice=(command == "NOTICE"))
            return

        # Membership events (join/part/quit/kick/nick) — surfaced to the agent's
        # channel context when show_events is on. ircstates has already updated
        # state in parse_tokens; we only narrate.
        if self.cfg.show_events and command in ("JOIN", "PART", "QUIT", "KICK", "NICK"):
            await self._handle_membership_event(command, line, pre_channels)
            return

        # WHOIS reply numerics -> fulfil a pending whois() future.
        if command in ("311","312","313","317","319","330","671","318","401","402","406") and len(line.params) >= 2:
            self._collect_whois(command, line.params)
            return

        # BATCH open/close — track draft/chathistory batches (Feature C).
        if command == "BATCH" and line.params:
            ref = line.params[0]
            if ref.startswith("+"):
                btype = line.params[1] if len(line.params) > 1 else ""
                if btype in ("chathistory", "draft/chathistory"):
                    self._chathistory_batches.add(ref[1:])
            elif ref.startswith("-"):
                self._chathistory_batches.discard(ref[1:])
            return

        # RPL_ENDOFNAMES (366) — channel fully joined; pull history (Feature C).
        if command == "366" and len(line.params) >= 2:
            await self._request_chathistory(line.params[1])
            return

    def _collect_whois(self, command, params):
        nick = params[1]
        slot = self._whois_pending.get(self.casefold(nick))
        if slot is None:
            return
        d = slot["data"]
        if command == "311" and len(params) >= 6:
            slot["found"] = True
            d["nick"] = params[1]; d["username"] = params[2]
            d["host"] = params[3]; d["realname"] = params[5]
        elif command == "312" and len(params) >= 4:
            d["server"] = params[2]; d["server_info"] = params[3]
        elif command == "313":
            d["operator"] = True
        elif command == "317" and len(params) >= 3:
            try: d["idle_seconds"] = int(params[2])
            except (TypeError, ValueError): pass
            if len(params) >= 4:
                try: d["signon_time"] = int(params[3])
                except (TypeError, ValueError): pass
        elif command == "319":
            d["channels"] = params[-1].split()
        elif command == "330" and len(params) >= 3:
            d["account"] = params[2]
        elif command == "671":
            d["secure"] = True
        elif command in ("401","402","406"):
            slot["found"] = False
            slot["event"].set()
        elif command == "318":
            slot["event"].set()

    def _user_shared_channels(self, nick: str) -> List[str]:
        """Channels (display names) the given user currently shares with us.

        Read from ircstates *before* a QUIT/NICK is parsed, so we know which
        channel contexts the event belongs to.
        """
        try:
            u = self.server.users.get(self.casefold(nick))
            if not u:
                return []
            out = []
            for ch_cf in getattr(u, "channels", set()) or set():
                ch = self.server.channels.get(ch_cf)
                out.append(getattr(ch, "name", ch_cf) if ch else ch_cf)
            return out
        except Exception:
            return []

    async def _handle_membership_event(self, command: str, line: Any,
                                       pre_channels: Optional[List[str]]) -> None:
        actor = line.hostmask.nickname if line.hostmask else (
            line.source.split("!", 1)[0] if line.source else "")
        if not actor:
            return
        params = line.params
        server_time = line.tags.get("time") if line.tags else None
        batch_ref = line.tags.get("batch") if line.tags else None
        is_history = bool(batch_ref and batch_ref in self._chathistory_batches)
        is_self = self.casefold_equals(actor, self.current_nick)

        if command == "JOIN":
            if is_self:
                return  # don't narrate our own joins
            channel = params[0] if params else ""
            if channel:
                await self._emit_event(channel, f"*** {actor} has joined {channel}",
                                       server_time, is_history)
        elif command == "PART":
            if is_self:
                return
            channel = params[0] if params else ""
            reason = params[1] if len(params) > 1 else ""
            if channel:
                text = f"*** {actor} has left {channel}" + (f" ({reason})" if reason else "")
                await self._emit_event(channel, text, server_time, is_history)
        elif command == "KICK":
            channel = params[0] if params else ""
            target = params[1] if len(params) > 1 else ""
            reason = params[2] if len(params) > 2 else ""
            if channel and target:
                text = (f"*** {target} was kicked from {channel} by {actor}"
                        + (f" ({reason})" if reason else ""))
                await self._emit_event(channel, text, server_time, is_history)
        elif command == "QUIT":
            if is_self:
                return
            reason = params[0] if params else ""
            text = f"*** {actor} has quit IRC" + (f" ({reason})" if reason else "")
            for channel in (pre_channels or []):
                await self._emit_event(channel, text, server_time, is_history)
        elif command == "NICK":
            new_nick = params[0] if params else ""
            if not new_nick or self.casefold_equals(new_nick, self.current_nick):
                return  # our own rename: parse_tokens already set current_nick
            text = f"*** {actor} is now known as {new_nick}"
            for channel in (pre_channels or []):
                await self._emit_event(channel, text, server_time, is_history)

    async def _emit_event(self, channel: str, text: str,
                          server_time: Optional[str], is_history: bool) -> None:
        if not self.is_channel(channel):
            return
        await self._on_message({
            "type": "event",
            "sender_nick": "*",
            "account": None,
            "target": channel,
            "text": text,
            "is_notice": False,
            "is_channel": True,
            "msgid": None,
            "server_time": server_time,
            "is_history": is_history,
            "tags": {},
        })

    async def _request_chathistory(self, channel: str) -> None:
        """Fetch recent backlog on (re)join where the server supports it."""
        if not self.has_cap("draft/chathistory"):
            return
        try:
            self.send_line("CHATHISTORY", ["LATEST", channel, "*", str(self.cfg.chathistory_limit)])
        except Exception:
            pass

    async def _handle_nick_in_use(self) -> None:
        # Post-registration, a 433 means a *regain* attempt (in the keepalive
        # loop) lost the race — someone still holds our configured nick. Don't
        # mangle our current working nick; just let the next regain tick retry.
        if self._registered_evt.is_set():
            logger.debug("IRCX: nick regain failed (433); will retry later")
            return
        # During registration, try the next suffixed candidate.
        self._nick_attempt += 1
        base = self.cfg.nickname.rstrip("_0123456789")[:24] or "hermes-bot"
        if self._nick_attempt == 1:
            candidate = f"{base}_"
        else:
            candidate = f"{base}_{self._nick_attempt}"
        self._desired_nick = candidate[:30]
        self.send_line("NICK", [self._desired_nick])

    def _maybe_regain_nick(self) -> None:
        """Reclaim our configured nick if we drifted off it (netsplit, forced
        change, collision-suffix during registration). Best-effort: we send a
        NICK and let the server confirm; a 433 just means try again next tick.
        """
        try:
            want = self.cfg.nickname
            have = self.current_nick
            if want and have and not self.casefold_equals(want, have):
                logger.info("IRCX: attempting to regain nick %s (currently %s)", want, have)
                self.send_line("NICK", [want])
        except Exception:
            pass

    # ---- CAP / SASL -------------------------------------------------------

    async def _handle_cap(self, line: Any) -> None:
        # Format: CAP <client> <subcmd> [*] :<caps>
        if len(line.params) < 2:
            return
        subcmd = line.params[1].upper()

        if subcmd == "LS":
            more = len(line.params) >= 4 and line.params[2] == "*"
            caps_str = line.params[-1]
            self._cap_ls_buffer.append(caps_str)
            if more:
                return
            for token in " ".join(self._cap_ls_buffer).split():
                if "=" in token:
                    name, value = token.split("=", 1)
                else:
                    name, value = token, ""
                self._cap_available[name] = value
            self._cap_ls_buffer.clear()
            await self._request_caps()
        elif subcmd == "ACK":
            for cap in line.params[-1].split():
                cap = cap.strip()
                if cap:
                    self._cap_acked.add(cap.lstrip("-"))
            if "sasl" in self._cap_acked and self.cfg.sasl_mechanism and not self._sasl_done_evt.is_set():
                await self._begin_sasl()
            else:
                self._maybe_end_cap()
        elif subcmd == "NAK":
            # Server refused our requested caps; proceed without them.
            self._maybe_end_cap()
        elif subcmd == "NEW":  # cap-notify: new caps offered post-registration
            for token in line.params[-1].split():
                name = token.split("=", 1)[0]
                self._cap_available[name] = ""
            wanted = [c for c in DESIRED_CAPS if c in self._cap_available and c not in self._cap_acked]
            if wanted:
                self.send_line("CAP", ["REQ", " ".join(wanted)])
        elif subcmd == "DEL":
            for token in line.params[-1].split():
                self._cap_acked.discard(token)

    async def _request_caps(self) -> None:
        wanted = [c for c in DESIRED_CAPS if c in self._cap_available]
        if self.cfg.sasl_mechanism and "sasl" not in wanted and "sasl" in self._cap_available:
            wanted.append("sasl")
        if not wanted:
            self._maybe_end_cap()
            return
        self._cap_requested = set(wanted)
        self.send_line("CAP", ["REQ", " ".join(wanted)])

    def _maybe_end_cap(self) -> None:
        if self._cap_negotiating and not self._registered_evt.is_set():
            self._cap_negotiating = False
            self.send_line("CAP", ["END"])

    async def _begin_sasl(self) -> None:
        mech = (self.cfg.sasl_mechanism or "PLAIN").upper()
        hash_name = _scram_hash_for(mech)
        if hash_name:
            self._scram = ScramClient(
                self.cfg.sasl_username or self.cfg.nickname,
                self.cfg.sasl_password or "",
                hash_name,
            )
        self.send_line("AUTHENTICATE", [mech])

    async def _handle_authenticate(self, line: Any) -> None:
        param = line.params[0] if line.params else "+"
        mech = (self.cfg.sasl_mechanism or "PLAIN").upper()

        if mech == "EXTERNAL":
            if param == "+":
                self.send_line("AUTHENTICATE", ["+"])  # authzid empty
            return

        if mech == "PLAIN":
            if param == "+":
                payload = sasl_plain_payload(
                    self.cfg.sasl_username or self.cfg.nickname,
                    self.cfg.sasl_password or "",
                )
                for chunk in _chunk_authenticate(payload):
                    self.send_line("AUTHENTICATE", [chunk])
            return

        if _scram_hash_for(mech) and self._scram is not None:
            if param == "+":
                payload = base64.b64encode(self._scram.client_first()).decode("ascii")
                for chunk in _chunk_authenticate(payload):
                    self.send_line("AUTHENTICATE", [chunk])
            else:
                data = base64.b64decode(param)
                if b"v=" in data and self._scram._server_signature:
                    # server-final
                    if self._scram.verify_server_final(data):
                        self.send_line("AUTHENTICATE", ["+"])
                    else:
                        self._sasl_error = "SCRAM: server signature mismatch"
                        self.send_line("AUTHENTICATE", ["*"])  # abort
                else:
                    # server-first -> client-final
                    final = self._scram.client_final(data)
                    payload = base64.b64encode(final).decode("ascii")
                    for chunk in _chunk_authenticate(payload):
                        self.send_line("AUTHENTICATE", [chunk])
            return

    def _finish_sasl(self) -> None:
        if not self._sasl_done_evt.is_set():
            self._sasl_done_evt.set()
            self._maybe_end_cap()

    # ---- inbound messages -------------------------------------------------

    async def _handle_privmsg(self, line: Any, *, is_notice: bool) -> None:
        if not line.source or not line.params:
            return
        sender_nick = line.hostmask.nickname if line.hostmask else line.source.split("!", 1)[0]
        target = line.params[0]
        text = line.params[1] if len(line.params) > 1 else ""

        # Ignore our own echoed messages (echo-message capability).
        if self.casefold_equals(sender_nick, self.current_nick):
            return

        # CTCP handling.
        if text.startswith("\x01"):
            handled = await self._handle_ctcp(sender_nick, target, text, is_notice=is_notice)
            if handled is not None:
                text = handled  # ACTION rendered as text
            else:
                return  # other CTCP consumed

        # Verified account (account-tag) if present.
        account = line.tags.get("account") if line.tags else None
        if not account:
            user = self.server.users.get(self.casefold(sender_nick)) if hasattr(self.server, "users") else None
            account = getattr(user, "account", None) if user else None

        server_time = line.tags.get("time") if line.tags else None
        msgid = line.tags.get("msgid") if line.tags else None

        # draft/chathistory replay: messages inside a chathistory batch are
        # backlog, not live — flag so the adapter buffers/logs but never
        # answers them (Feature C).
        batch_ref = line.tags.get("batch") if line.tags else None
        is_history = bool(batch_ref and batch_ref in self._chathistory_batches)

        await self._on_message(
            {
                "sender_nick": sender_nick,
                "account": account or None,
                "target": target,
                "text": text,
                "is_notice": is_notice,
                "is_channel": self.is_channel(target),
                "msgid": msgid,
                "server_time": server_time,
                "is_history": is_history,
                "tags": dict(line.tags) if line.tags else {},
            }
        )

    async def _handle_ctcp(self, sender_nick: str, target: str, text: str, *, is_notice: bool) -> Optional[str]:
        body = text.strip("\x01")
        parts = body.split(" ", 1)
        ctcp_cmd = parts[0].upper()
        arg = parts[1] if len(parts) > 1 else ""

        if ctcp_cmd == "ACTION":
            return f"* {sender_nick} {arg}"

        # Don't reply to CTCP arriving in NOTICE (those are replies already).
        if is_notice:
            return None

        replies = {
            "VERSION": _CTCP_VERSION,
            "SOURCE": "https://github.com/NousResearch/hermes-agent",
            "CLIENTINFO": "ACTION CLIENTINFO PING SOURCE TIME VERSION",
            "PING": arg,
            "TIME": datetime.datetime.now().strftime("%a %b %d %H:%M:%S %Y"),
        }
        if ctcp_cmd in replies:
            self.send_line("NOTICE", [sender_nick, f"\x01{ctcp_cmd} {replies[ctcp_cmd]}\x01".rstrip()])
        return None

    # ---- outbound messaging ----------------------------------------------

    def privmsg(self, target: str, text: str, *, reply_to: Optional[str] = None) -> None:
        tags: Optional[Dict[str, str]] = None
        if reply_to and self.has_cap("message-tags"):
            tags = {"+draft/reply": reply_to}
        self.send_line("PRIVMSG", [target, strip_irc_control_chars(text)], tags=tags)

    def send_typing(self, target: str, state: str = "active") -> None:
        if self.has_cap("message-tags"):
            self.send_line("TAGMSG", [target], tags={"+typing": state})

    # ---- runtime channel agency (Feature B) ------------------------------

    def join(self, channel: str, key: Optional[str] = None) -> None:
        self.send_line("JOIN", [channel, key] if key else [channel])

    def part(self, channel: str, reason: Optional[str] = None) -> None:
        self.send_line("PART", [channel, reason] if reason else [channel])

    def notice(self, target: str, text: str) -> None:
        self.send_line("NOTICE", [target, strip_irc_control_chars(text)])

    def set_topic(self, channel: str, topic: str) -> None:
        self.send_line("TOPIC", [channel, strip_irc_control_chars(topic)])

    def request_topic(self, channel: str) -> None:
        self.send_line("TOPIC", [channel])

    def set_nick(self, nick: str) -> None:
        self.send_line("NICK", [nick])

    def request_modes(self, channel: str) -> None:
        self.send_line("MODE", [channel])

    def set_modes(self, channel: str, modestring: str, args: Optional[List[str]] = None) -> None:
        self.send_line("MODE", [channel, modestring] + (args or []))

    def kick(self, channel: str, nick: str, reason: Optional[str] = None) -> None:
        self.send_line("KICK", [channel, nick, reason] if reason else [channel, nick])

    def channel_modes(self, channel: str) -> Optional[List[str]]:
        try:
            ch = self.server.channels.get(self.casefold(channel))
            if ch is None:
                return None
            return sorted(getattr(ch, "modes", {}) or {})
        except Exception:
            return None

    def am_i_op(self, channel: str) -> bool:
        """True if the bot currently holds op (+o) in *channel*."""
        try:
            ch = self.server.channels.get(self.casefold(channel))
            if ch is None:
                return False
            me = ch.users.get(self.casefold(self.current_nick))
            return bool(me and "o" in (getattr(me, "modes", set()) or set()))
        except Exception:
            return False

    def set_away(self, message):
        if message:
            self._away_message = message
            self.send_line("AWAY", [strip_irc_control_chars(message)])
        else:
            self._away_message = None
            self.send_line("AWAY")

    @property
    def away_message(self):
        return self._away_message

    async def whois(self, nick, timeout=8.0):
        """Issue a real WHOIS and await the reply (works network-wide)."""
        key = self.casefold(nick)
        evt = asyncio.Event()
        slot = {"data": {"nick": nick}, "event": evt, "found": False}
        self._whois_pending[key] = slot
        try:
            self.send_line("WHOIS", [nick])
            try:
                await asyncio.wait_for(evt.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return None
            return slot["data"] if slot["found"] else None
        finally:
            self._whois_pending.pop(key, None)

    def joined_channels(self) -> List[str]:
        try:
            return [getattr(c, "name", k) for k, c in self.server.channels.items()]
        except Exception:
            return []

    def in_channel(self, channel: str) -> bool:
        try:
            return self.server.has_channel(channel)
        except Exception:
            cf = self.casefold(channel)
            return any(self.casefold(c) == cf for c in self.joined_channels())

    def _prefix_for_modes(self, modes) -> str:
        """Map membership modes (e.g. {'o','v'}) to sigils (@, +) via ISUPPORT PREFIX."""
        try:
            pfx = self.server.isupport.prefix
            order = list(getattr(pfx, "modes", "ov"))
            sig = list(getattr(pfx, "prefixes", "@+"))
            mapping = dict(zip(order, sig))
            for m in order:
                if modes and m in modes:
                    return mapping.get(m, "")
        except Exception:
            if modes:
                if "o" in modes:
                    return "@"
                if "v" in modes:
                    return "+"
        return ""

    def channel_info(self, channel: str) -> Optional[Dict[str, Any]]:
        """Read membership/ops/voice/topic for a joined channel from ircstates."""
        try:
            ch = self.server.channels.get(self.casefold(channel))
        except Exception:
            ch = None
        if ch is None:
            return None
        members = []
        ops = []
        voiced = []
        for nick_cf, cu in getattr(ch, "users", {}).items():
            user = self.server.users.get(nick_cf)
            nick = user.nickname if user else nick_cf
            modes = getattr(cu, "modes", set()) or set()
            sig = self._prefix_for_modes(modes)
            members.append(f"{sig}{nick}")
            if "o" in modes:
                ops.append(nick)
            elif "v" in modes:
                voiced.append(nick)
        members.sort(key=lambda n: (n[:1] not in "@+", n.lstrip("@+%&~").lower()))
        return {
            "channel": getattr(ch, "name", channel),
            "topic": getattr(ch, "topic", None),
            "topic_setter": getattr(ch, "topic_setter", None),
            "user_count": len(members),
            "op_count": len(ops),
            "voice_count": len(voiced),
            "ops": ops,
            "voiced": voiced,
            "members": members,
            "channel_modes": sorted(getattr(ch, "modes", {}) or {}),
        }

    def refresh_names(self, channel: str) -> None:
        """Ask the server to re-send the NAMES list for a channel."""
        if self.is_channel(channel):
            self.send_line("NAMES", [channel])

    def user_info(self, nick: str) -> Optional[Dict[str, Any]]:
        """Best-effort info about a user already known in shared-channel state."""
        try:
            u = self.server.users.get(self.casefold(nick))
        except Exception:
            u = None
        if u is None:
            return None
        shared = []
        try:
            for ch_cf, ch in self.server.channels.items():
                if self.casefold(nick) in getattr(ch, "users", {}):
                    shared.append(getattr(ch, "name", ch_cf))
        except Exception:
            pass
        return {
            "nick": u.nickname,
            "username": getattr(u, "username", None),
            "hostname": getattr(u, "hostname", None),
            "realname": getattr(u, "realname", None),
            "account": getattr(u, "account", None),
            "away": getattr(u, "away", None),
            "shared_channels": shared,
        }


# ===========================================================================
# Hermes platform adapter
# ===========================================================================

class IRCXAdapter(BasePlatformAdapter):
    """IRCv3 adapter implementing the BasePlatformAdapter contract."""

    def __init__(self, config: Any, **kwargs: Any):
        super().__init__(config=config, platform=Platform("ircx"))
        self.cfg = load_config(config)
        self._client: Optional[IRCClient] = None
        self._lock_key: Optional[str] = None
        # Rolling per-channel context buffers (deques of "nick: text").
        from collections import deque as _deque
        self._buffers: Dict[str, "Any"] = {}
        self._deque = _deque
        self._last_spontaneous: Dict[str, float] = {}
        self._ignored = {}

    @property
    def name(self) -> str:
        return "IRC"

    # ---- context buffer + logging (Features A & C) -----------------------

    def _buf(self, chat_id: str):
        cf = self._client.casefold(chat_id) if self._client else chat_id.lower()
        if cf not in self._buffers:
            self._buffers[cf] = self._deque(maxlen=max(1, self.cfg.context_buffer_size))
            self._seed_buffer_from_log(chat_id, self._buffers[cf])
        return self._buffers[cf]

    def _record_line(self, chat_id: str, sender: str, text: str) -> None:
        self._buf(chat_id).append(f"{sender}: {text}")
        # Disk append is offloaded so it never blocks the asyncio event loop
        # (this runs in the recv path on every message). Fire-and-forget on
        # the loop's default executor; failures are swallowed inside the job.
        path = self._log_path(chat_id)
        if not path:
            return
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        line = f"{ts}\t{sender}\t{text}\n"
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _write_log_line, path, line)
        except RuntimeError:
            # No running loop (e.g. unit tests calling _record_line directly):
            # fall back to a direct write — there's no loop to block.
            _write_log_line(path, line)

    def _record_event_msg(self, msg: Dict[str, Any]) -> None:
        """Record a membership event into the channel's context buffer."""
        if not self.cfg.show_events:
            return
        target = msg.get("target")
        text = msg.get("text") or ""
        if not target or not text:
            return
        # Respect allowlist group policy, same as messages.
        if self.cfg.group_policy == "allowlist" and self._channel_spec(target) is None:
            return
        self._record_event(target, text)

    def _record_event(self, chat_id: str, text: str) -> None:
        """Append a pre-formatted event line (e.g. '*** X has joined') to the
        rolling context buffer (and the on-disk log), with no 'sender:' prefix."""
        self._buf(chat_id).append(text)
        path = self._log_path(chat_id)
        if not path:
            return
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        line = f"{ts}\t*\t{text}\n"
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _write_log_line, path, line)
        except RuntimeError:
            _write_log_line(path, line)

    def _format_context(self, chat_id: str) -> Optional[str]:
        buf = self._buffers.get(self._client.casefold(chat_id) if self._client else chat_id.lower())
        if not buf:
            return None
        lines = list(buf)[:-1]  # exclude the triggering line itself
        if not lines:
            return None
        return "Recent channel conversation:\n" + "\n".join(lines)

    def _log_path(self, chat_id: str) -> Optional["Any"]:
        if not self.cfg.log_dir:
            return None
        import os as _os
        safe = re.sub(r"[^A-Za-z0-9#&_.-]", "_", f"{self.cfg.server}_{chat_id}")
        d = self.cfg.log_dir
        try:
            _os.makedirs(d, exist_ok=True)
        except Exception:
            return None
        return _os.path.join(d, safe + ".log")

    def _append_log(self, chat_id: str, sender: str, text: str) -> None:
        """Synchronous append (used by tests / non-loop callers).

        The hot path in ``_record_line`` offloads writes to an executor so the
        event loop never blocks on disk; this direct version is retained for
        callers that aren't on the loop.
        """
        path = self._log_path(chat_id)
        if not path:
            return
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        _write_log_line(path, f"{ts}\t{sender}\t{text}\n")

    def _seed_buffer_from_log(self, chat_id: str, buf) -> None:
        """Replay the tail of the on-disk log so context survives restarts."""
        path = self._log_path(chat_id)
        if not path:
            return
        try:
            import os as _os
            if not _os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as fh:
                tail = fh.readlines()[-buf.maxlen:]
            for row in tail:
                parts = row.rstrip("\n").split("\t", 2)
                if len(parts) == 3:
                    buf.append(f"{parts[1]}: {parts[2]}")
        except Exception:
            pass

    # ---- lifecycle --------------------------------------------------------

    async def connect(self) -> bool:
        if not _LIBS_OK:
            self._set_fatal_error("deps_missing", f"{INSTALL_HINT} ({_LIBS_ERR})", retryable=False)
            return False
        if not self.cfg.server or not self.cfg.channels:
            self._set_fatal_error("config_missing", "IRCX_SERVER and IRCX_CHANNEL must be set", retryable=False)
            return False

        # Prevent two profiles from claiming the same IRC identity.
        if not self._acquire_platform_lock("ircx", f"{self.cfg.server}:{self.cfg.nickname}",
                                           f"{self.cfg.nickname}@{self.cfg.server}"):
            self._set_fatal_error("lock_conflict", "IRC identity in use by another profile", retryable=False)
            return False

        self._client = IRCClient(
            self.cfg,
            on_message=self._on_irc_message,
            on_disconnect=self._on_irc_disconnect,
        )
        try:
            await self._client.connect()
        except Exception as exc:
            logger.error("IRCX: connect failed: %s", exc)
            retryable = not isinstance(exc, ssl.SSLCertVerificationError)
            self._set_fatal_error("connect_failed", str(exc), retryable=retryable)
            self._release_platform_lock()
            return False

        self._mark_connected()
        logger.info(
            "IRCX: connected to %s:%s as %s; joined %s",
            self.cfg.server, self.cfg.port, self._client.current_nick,
            ", ".join(self.cfg.channel_names()),
        )
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        self._release_platform_lock()

    async def _on_irc_disconnect(self, reason: str) -> None:
        if not self.is_connected:
            return
        logger.warning("IRCX: %s — marking disconnected for reconnect", reason)
        self._set_fatal_error("connection_lost", f"IRC connection lost: {reason}", retryable=True)
        await self._notify_fatal_error()

    # ---- authorization & mention gating ----------------------------------

    def _channel_spec(self, target: str) -> Optional[ChannelSpec]:
        if not self._client:
            return None
        return self.cfg.channel_spec(self._client.casefold(target), self._client.casefold)

    def _require_mention_for(self, target: str) -> bool:
        spec = self._channel_spec(target)
        if spec is not None and spec.require_mention is not None:
            return spec.require_mention
        return self.cfg.require_mention

    def _strip_mention(self, text: str) -> Tuple[str, bool]:
        """Return (text_without_mention, was_addressed)."""
        nick = self._client.current_nick if self._client else self.cfg.nickname
        candidates = [nick] + list(self.cfg.mention_aliases)
        for name in candidates:
            for sep in (":", ",", " "):
                prefix = f"{name}{sep}"
                if text.lower().startswith(prefix.lower()):
                    return text[len(prefix):].strip(), True
        return text, False

    def _resolve_identity(self, sender_nick: str, account: Optional[str]) -> Optional[str]:
        """Identity used for authorization.

        Prefer the network-verified account.  Fall back to the bare nick
        only when ``dangerously_allow_name_matching`` is enabled.
        """
        if account:
            return account
        if self.cfg.dangerously_allow_name_matching:
            return sender_nick
        return None

    def _is_authorized(self, identity: Optional[str], is_channel: bool, target: str) -> bool:
        # allow-all (env or extra) short-circuit
        if _truthy(_env("IRCX_ALLOW_ALL_USERS", "IRC_ALLOW_ALL_USERS") or ""):
            return True
        if identity is None:
            return False
        ident_l = identity.lower()

        if is_channel:
            spec = self._channel_spec(target)
            allow = spec.allow_from if (spec and spec.allow_from is not None) else self.cfg.group_allow_from
            # An explicit (possibly empty) channel allowlist is authoritative.
            if spec and spec.allow_from is not None:
                return ident_l in {a.lower() for a in allow}
            if self.cfg.group_allow_from:
                return ident_l in {a.lower() for a in self.cfg.group_allow_from}
            # No channel allowlist configured: fall through to global list.
        else:
            if self.cfg.allow_from:
                return ident_l in {a.lower() for a in self.cfg.allow_from}

        env_list = _coerce_str_list(_env("IRCX_ALLOWED_USERS", "IRC_ALLOWED_USERS"))
        if env_list:
            return ident_l in {a.lower() for a in env_list}
        # No adapter-side allowlist configured — defer to the gateway's
        # central _is_user_authorized (pairing / global allow-all).
        return True

    def _resolve_tool_scope(
        self, identity: Optional[str], is_channel: bool, target: str
    ) -> Optional[List[str]]:
        """Per-channel / per-sender toolset allowlist for this turn.

        Mirrors OpenClaw ``groups.<chan>.tools`` / ``tools_by_sender``.
        Entries are Hermes *toolset* names (e.g. ``hermes-cli``, ``web``,
        ``memory``).  ``tools_by_sender`` (matched on the resolved identity,
        case-insensitive) takes precedence over the channel-wide ``tools``.
        Returns ``None`` (or ``["*"]``) for no restriction; the gateway
        intersects this turn's enabled toolsets with the result.
        """
        if not is_channel:
            return None
        spec = self._channel_spec(target)
        if spec is None:
            return None
        if identity and spec.tools_by_sender:
            lowered = {k.lower(): v for k, v in spec.tools_by_sender.items()}
            scoped = lowered.get(identity.lower())
            if scoped is not None:
                return scoped
        return spec.tools

    # ---- inbound dispatch -------------------------------------------------

    async def _on_irc_message(self, msg: Dict[str, Any]) -> None:
        # Membership events: record to channel context only, never dispatch to
        # the agent as a prompt (pure visibility).
        if msg.get("type") == "event":
            self._record_event_msg(msg)
            return
        if not self._message_handler:
            return
        if msg["is_notice"]:
            return  # never treat NOTICE as a prompt (loop-safety)

        sender_nick = msg["sender_nick"]
        text = msg["text"]
        is_channel = msg["is_channel"]
        target = msg["target"]
        spontaneous = False

        # Temporary mute: drop everything from an ignored nick — not recorded
        # for context, not answered.  Replayed history is exempt (it predates
        # the mute and is never answered anyway).
        if not msg.get("is_history") and self._is_ignored(sender_nick):
            return

        if is_channel:
            # Denylist: never engage in a blocked channel — not recorded, not
            # answered — even if the bot somehow ends up in it (e.g. forced join).
            if self._is_blocked_channel(target):
                return
            # group_policy: in allowlist mode only handle configured channels.
            if self.cfg.group_policy == "allowlist" and self._channel_spec(target) is None:
                return

            # Record every channel line for rolling context + logging.  Backlog
            # replayed via draft/chathistory is recorded but never answered.
            self._record_line(target, sender_nick, text)
            if msg.get("is_history"):
                return

            if self._require_mention_for(target):
                text, addressed = self._strip_mention(text)
                if not addressed:
                    # Unaddressed chatter.  In observe mode the bot may
                    # spontaneously chime in (probability + per-channel
                    # cooldown); otherwise it stays quiet.
                    if not self._should_chime_in(target):
                        return
                    spontaneous = True
            chat_id = target
            chat_type = "group"
        else:
            chat_id = sender_nick
            chat_type = "dm"
            self._record_line(chat_id, sender_nick, text)
            if msg.get("is_history"):
                return

        identity = self._resolve_identity(sender_nick, msg.get("account"))
        if not self._is_authorized(identity, is_channel, target):
            logger.debug("IRCX: dropping message from unauthorized %s (account=%s)", sender_nick, msg.get("account"))
            return

        # Feed the verified account (or nick) as user_id so the gateway's
        # central _is_user_authorized matches IRCX_ALLOWED_USERS correctly.
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_id,
            chat_type=chat_type,
            user_id=identity or sender_nick,
            user_name=sender_nick,
            message_id=msg.get("msgid"),
        )

        ts = datetime.datetime.now()
        if msg.get("server_time"):
            try:
                ts = datetime.datetime.fromisoformat(msg["server_time"].replace("Z", "+00:00"))
            except Exception:
                pass

        effective_text = text
        if spontaneous:
            # Frame the turn so the agent contributes naturally and may opt
            # to stay silent.
            effective_text = (
                f"[ambient channel message from {sender_nick}] {text}\n\n"
                "(You are observing the channel and may optionally contribute a "
                "brief, relevant remark. If you have nothing useful to add, reply "
                "with exactly: <silent>)"
            )

        event = MessageEvent(
            text=effective_text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=msg.get("msgid") or str(int(time.time() * 1000)),
            timestamp=ts,
            channel_context=self._format_context(chat_id) if is_channel else None,
        )
        # Attach tool scope as an instance attribute (not a constructor kwarg)
        # so the plugin works on BOTH stock Hermes — where MessageEvent has no
        # ``tool_scope`` field, so it's simply ignored — and patched Hermes,
        # where CORE_PATCH.md adds the field + ``_apply_tool_scope`` enforcement.
        event.tool_scope = self._resolve_tool_scope(identity, is_channel, target)
        await self.handle_message(event)

    def _should_chime_in(self, target: str) -> bool:
        """Probability + per-channel cooldown gate for spontaneous replies."""
        if not self.cfg.observe_mode or self.cfg.spontaneous_probability <= 0:
            return False
        now = time.monotonic()
        last = self._last_spontaneous.get(target.lower())
        # Only apply the cooldown once we've actually posted before. Using a
        # 0.0 sentinel was unsafe: time.monotonic() can be small on a freshly
        # booted host, wrongly blocking the very first spontaneous message.
        if last is not None and (now - last) < self.cfg.spontaneous_cooldown:
            return False
        if secrets.SystemRandom().random() >= self.cfg.spontaneous_probability:
            return False
        self._last_spontaneous[target.lower()] = now
        return True

    # ---- outbound (BasePlatformAdapter contract) -------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._client or not self.is_connected:
            return SendResult(success=False, error="Not connected", retryable=True)
        if any(ch in chat_id for ch in ("\r", "\n", "\x00", " ")):
            return SendResult(success=False, error="Illegal characters in chat_id")

        # Observe-mode silence opt-out: the agent declined to contribute.
        if content.strip() in ("<silent>", "&lt;silent&gt;"):
            return SendResult(success=True, message_id="silent")

        lines = split_message(
            content, chat_id, self.cfg.max_message_length,
            convert_formatting=self.cfg.convert_formatting,
        )
        if not lines:
            return SendResult(success=False, error="Empty message after formatting")

        try:
            for i, line in enumerate(lines):
                self._client.privmsg(chat_id, line, reply_to=reply_to if i == 0 else None)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

        # Keep our own replies in the rolling context (echo-message would
        # otherwise be filtered out as self).
        if self._client.is_channel(chat_id):
            self._record_line(chat_id, self._client.current_nick, " ".join(lines))

        return SendResult(success=True, message_id=str(int(time.time() * 1000)))

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        if self._client and self.is_connected:
            try:
                self._client.send_typing(chat_id, "active")
            except Exception:
                pass

    async def stop_typing(self, chat_id: str) -> None:
        if self._client and self.is_connected:
            try:
                self._client.send_typing(chat_id, "done")
            except Exception:
                pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        is_channel = self._client.is_channel(chat_id) if self._client else chat_id[:1] in "#&+!"
        info: Dict[str, Any] = {"name": chat_id, "type": "group" if is_channel else "dm"}
        if self._client and is_channel:
            try:
                channel = self._client.server.channels.get(self._client.casefold(chat_id))
                if channel is not None:
                    info["topic"] = getattr(channel, "topic", None)
                    info["user_count"] = len(getattr(channel, "users", {}) or {})
            except Exception:
                pass
        return info

    def format_message(self, content: str) -> str:
        return markdown_to_irc(content) if self.cfg.convert_formatting else strip_markdown(content)

    # ---- runtime agency, driven by agent tools (Feature B) ---------------

    def _is_blocked_channel(self, channel: str) -> bool:
        """True if *channel* is on the IRCX_BLOCKED_CHANNELS denylist."""
        if not self.cfg.blocked_channels or not channel:
            return False
        return channel.lower() in {c.lower() for c in self.cfg.blocked_channels}

    def _join_allowed(self, channel: str) -> Optional[str]:
        """Return an error string if joining *channel* isn't permitted."""
        if not self.cfg.allow_agent_join:
            return "agent JOIN/PART is disabled (set IRCX_ALLOW_AGENT_JOIN=true to enable)"
        if not channel or channel[0] not in "#&+!":
            return f"invalid channel name: {channel!r}"
        if any(c in channel for c in ("\r", "\n", "\x00", " ")):
            return "channel name contains illegal characters"
        # Denylist wins over everything — never join a blocked channel, even on request.
        if self._is_blocked_channel(channel):
            return f"{channel} is on the IRCX_BLOCKED_CHANNELS denylist"
        if self.cfg.joinable_channels:
            allowed = {c.lower() for c in self.cfg.joinable_channels}
            if channel.lower() not in allowed:
                return f"{channel} is not in the IRCX_JOINABLE_CHANNELS allowlist"
        return None

    async def runtime_join(self, channel: str, key: Optional[str] = None) -> Dict[str, Any]:
        if not self._client or not self.is_connected:
            return {"error": "not connected"}
        err = self._join_allowed(channel)
        if err:
            return {"error": err}
        # Persist so the channel is rejoined automatically after a reconnect.
        if not any(self._client.casefold(c.name) == self._client.casefold(channel) for c in self.cfg.channels):
            self.cfg.channels.append(ChannelSpec(name=channel, key=key))
        self._client.join(channel, key)
        return {"success": True, "joined": channel}

    async def runtime_part(self, channel: str, reason: Optional[str] = None) -> Dict[str, Any]:
        if not self._client or not self.is_connected:
            return {"error": "not connected"}
        if not self.cfg.allow_agent_join:
            return {"error": "agent JOIN/PART is disabled (set IRCX_ALLOW_AGENT_JOIN=true)"}
        self.cfg.channels = [c for c in self.cfg.channels
                             if self._client.casefold(c.name) != self._client.casefold(channel)]
        # Drop per-channel state so leaving many channels over a long uptime
        # doesn't accumulate stale entries (spontaneous cooldown + context buf).
        self._last_spontaneous.pop(channel.lower(), None)
        try:
            self._buffers.pop(self._client.casefold(channel), None)
        except Exception:
            pass
        self._client.part(channel, reason)
        return {"success": True, "parted": channel}

    async def runtime_say(self, target: str, text: str) -> Dict[str, Any]:
        if not self._client or not self.is_connected:
            return {"error": "not connected"}
        if not target or any(c in target for c in ("\r", "\n", "\x00", " ")):
            return {"error": "invalid target"}
        # Only speak into channels we have actually joined, or to a nick (DM).
        if target[0] in "#&+!" and not self._client.in_channel(target):
            return {"error": f"not in channel {target} (join it first)"}
        res = await self.send(target, text)
        return {"success": True, "target": target} if res.success else {"error": res.error}

    def runtime_list(self) -> Dict[str, Any]:
        if not self._client:
            return {"channels": []}
        return {"channels": self._client.joined_channels(), "nick": self._client.current_nick}

    async def runtime_channel_info(self, channel: str) -> Dict[str, Any]:
        if not self._client or not self.is_connected:
            return {"error": "not connected"}
        target = channel or ""
        if not target and self.cfg.channels:
            target = self.cfg.channels[0].name
        if not target:
            return {"error": "no channel specified"}
        if not self._client.is_channel(target):
            return {"error": f"{target} is not a channel"}
        if not self._client.in_channel(target):
            return {"error": f"not in channel {target} (the bot must be a member to see its users)"}
        info = self._client.channel_info(target)
        if info is None:
            return {"error": f"no state for {target} yet — try again in a moment"}
        return {"success": True, **info}

    async def runtime_whois(self, nick: str) -> Dict[str, Any]:
        if not self._client or not self.is_connected:
            return {"error": "not connected"}
        if not nick:
            return {"error": "no nick specified"}
        info = self._client.user_info(nick)
        if info is None:
            return {"error": f"{nick} is not visible in any channel the bot shares"}
        return {"success": True, **info}

    async def runtime_topic(self, channel: str, topic: Optional[str] = None) -> Dict[str, Any]:
        if not self._client or not self.is_connected:
            return {"error": "not connected"}
        if not channel and self.cfg.channels:
            channel = self.cfg.channels[0].name
        if not self._client.is_channel(channel):
            return {"error": f"{channel} is not a channel"}
        if not self._client.in_channel(channel):
            return {"error": f"not in channel {channel}"}
        if topic is None:
            info = self._client.channel_info(channel)
            return {"success": True, "channel": channel, "topic": (info or {}).get("topic")}
        # Setting a topic may require +t op rights; the server will reject if so.
        self._client.set_topic(channel, topic)
        return {"success": True, "channel": channel, "topic_set": topic}

    async def runtime_notice(self, target: str, text: str) -> Dict[str, Any]:
        if not self._client or not self.is_connected:
            return {"error": "not connected"}
        if not target or any(c in target for c in ("\r", "\n", "\x00", " ")):
            return {"error": "invalid target"}
        if target[0] in "#&+!" and not self._client.in_channel(target):
            return {"error": f"not in channel {target}"}
        for line in split_message(text, target, self.cfg.max_message_length,
                                  convert_formatting=self.cfg.convert_formatting):
            self._client.notice(target, line)
        return {"success": True, "target": target}

    async def runtime_nick(self, nick: str) -> Dict[str, Any]:
        if not self._client or not self.is_connected:
            return {"error": "not connected"}
        nick = (nick or "").strip()
        if not nick or any(c in nick for c in ("\r", "\n", "\x00", " ", ",")) or nick[0] in "#&+!:":
            return {"error": "invalid nickname"}
        self._client.set_nick(nick)
        return {"success": True, "requested_nick": nick,
                "note": "the server confirms NICK changes asynchronously"}

    async def runtime_mode(self, channel: str, modestring: Optional[str] = None,
                           args: Optional[List[str]] = None) -> Dict[str, Any]:
        if not self._client or not self.is_connected:
            return {"error": "not connected"}
        if not channel and self.cfg.channels:
            channel = self.cfg.channels[0].name
        if not self._client.is_channel(channel):
            return {"error": f"{channel} is not a channel"}
        if not self._client.in_channel(channel):
            return {"error": f"not in channel {channel}"}
        if modestring is None:
            return {"success": True, "channel": channel,
                    "modes": self._client.channel_modes(channel)}
        # Changing modes requires the bot to be a channel op.
        if not self._client.am_i_op(channel):
            return {"error": f"cannot change modes: the bot is not an operator in {channel}"}
        clean = modestring.strip()
        if not clean or any(c in clean for c in ("\r", "\n", "\x00", " ")):
            return {"error": "invalid mode string"}
        safe_args = [a for a in (args or []) if not any(c in a for c in ("\r", "\n", "\x00", " "))]
        self._client.set_modes(channel, clean, safe_args)
        return {"success": True, "channel": channel, "mode_change": clean, "args": safe_args}

    async def runtime_kick(self, channel: str, nick: str, reason: Optional[str] = None) -> Dict[str, Any]:
        if not self._client or not self.is_connected:
            return {"error": "not connected"}
        if not self.cfg.allow_agent_kick:
            return {"error": "agent KICK is disabled (set IRCX_ALLOW_AGENT_KICK=true to enable)"}
        if not self._client.is_channel(channel) or not self._client.in_channel(channel):
            return {"error": f"not in channel {channel}"}
        if not nick or any(c in nick for c in ("\r", "\n", "\x00", " ")):
            return {"error": "invalid nick"}
        if self._client.casefold_equals(nick, self._client.current_nick):
            return {"error": "refusing to kick myself"}
        if not self._client.am_i_op(channel):
            return {"error": f"cannot kick: the bot is not an operator in {channel}"}
        self._client.kick(channel, nick, reason)
        return {"success": True, "channel": channel, "kicked": nick}

    async def runtime_accounts_online(self) -> Dict[str, Any]:
        """Which allowed users (by verified account or nick) are visible right now."""
        if not self._client or not self.is_connected:
            return {"error": "not connected"}
        allowed = _coerce_str_list(_env("IRCX_ALLOWED_USERS", "IRC_ALLOWED_USERS")) or list(self.cfg.allow_from)
        if not allowed:
            return {"success": True, "online": [], "note": "no allowed_users configured"}
        allowed_l = {a.lower() for a in allowed}
        online = []
        try:
            for nick_cf, user in self._client.server.users.items():
                acct = (getattr(user, "account", None) or "")
                nick = user.nickname
                if acct.lower() in allowed_l or nick.lower() in allowed_l:
                    online.append({"nick": nick, "account": acct or None,
                                   "away": getattr(user, "away", None)})
        except Exception:
            pass
        return {"success": True, "online": online, "count": len(online)}

    async def runtime_search_history(self, query: str, channel: str = "",
                                     sender: str = "", limit: int = 20) -> Dict[str, Any]:
        """Search the on-disk channel logs (requires IRCX_LOG_DIR)."""
        if not self.cfg.log_dir:
            return {"error": "history search needs logging enabled (set IRCX_LOG_DIR)"}
        target = channel or (self.cfg.channels[0].name if self.cfg.channels else "")
        if not target:
            return {"error": "no channel specified"}
        path = self._log_path(target)
        if not path:
            return {"error": "could not resolve log path"}
        q = (query or "").lower()
        snd = (sender or "").lower()
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 20

        def _search() -> List[Dict[str, str]]:
            import os as _os
            if not _os.path.exists(path):
                return []
            hits: List[Dict[str, str]] = []
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for row in fh:
                    parts = row.rstrip("\n").split("\t", 2)
                    if len(parts) != 3:
                        continue
                    ts, who, text = parts
                    if snd and who.lower() != snd:
                        continue
                    if q and q not in text.lower():
                        continue
                    hits.append({"time": ts, "sender": who, "text": text})
            return hits[-limit:]

        try:
            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(None, _search)
        except RuntimeError:
            results = _search()
        return {"success": True, "channel": target, "query": query,
                "match_count": len(results), "matches": results}


    async def runtime_away(self, message=None):
        if not self._client or not self.is_connected:
            return {"error": "not connected"}
        self._client.set_away(message or None)
        if message:
            return {"success": True, "away": True, "message": message}
        return {"success": True, "away": False}

    async def runtime_cycle(self, channel, reason=None):
        if not self._client or not self.is_connected:
            return {"error": "not connected"}
        if not self._client.is_channel(channel):
            return {"error": "%s is not a channel" % channel}
        if not self._client.in_channel(channel):
            return {"error": "not in channel %s" % channel}
        spec = self._channel_spec(channel)
        key = spec.key if spec else None
        self._client.part(channel, reason or "cycling")
        await asyncio.sleep(0.5)
        self._client.join(channel, key)
        return {"success": True, "cycled": channel}

    async def runtime_set_key(self, channel, key=None):
        if not self._client or not self.is_connected:
            return {"error": "not connected"}
        if not self._client.is_channel(channel) or not self._client.in_channel(channel):
            return {"error": "not in channel %s" % channel}
        if not self._client.am_i_op(channel):
            return {"error": "cannot set key: the bot is not an operator in %s" % channel}
        spec = self._channel_spec(channel)
        if key:
            if any(c in key for c in (chr(13), chr(10), chr(0), " ", ",")):
                return {"error": "invalid channel key"}
            self._client.set_modes(channel, "+k", [key])
            if spec is not None:
                spec.key = key
            return {"success": True, "channel": channel, "key_set": True}
        else:
            old = spec.key if spec else None
            self._client.set_modes(channel, "-k", [old] if old else [])
            if spec is not None:
                spec.key = None
            return {"success": True, "channel": channel, "key_cleared": True}

    async def runtime_whois_server(self, nick):
        if not self._client or not self.is_connected:
            return {"error": "not connected"}
        nick = (nick or "").strip()
        if not nick or any(c in nick for c in (chr(13), chr(10), chr(0), " ")):
            return {"error": "invalid nick"}
        data = await self._client.whois(nick)
        if data is None:
            return {"error": "%s is not online (or WHOIS timed out)" % nick}
        return {"success": True, **data}

    async def runtime_ignore(self, nick, seconds=300):
        if not nick:
            return {"error": "no nick specified"}
        try:
            seconds = max(1, min(int(seconds), 86400))
        except (TypeError, ValueError):
            seconds = 300
        import time as _t
        self._ignored[nick.lower()] = _t.monotonic() + seconds
        return {"success": True, "ignored": nick, "seconds": seconds}

    async def runtime_unignore(self, nick):
        if not nick:
            return {"error": "no nick specified"}
        existed = self._ignored.pop(nick.lower(), None) is not None
        return {"success": True, "nick": nick, "was_ignored": existed}

    def _is_ignored(self, nick):
        import time as _t
        exp = self._ignored.get((nick or "").lower())
        if exp is None:
            return False
        if _t.monotonic() >= exp:
            self._ignored.pop(nick.lower(), None)
            return False
        return True

# ===========================================================================
# Plugin module-level hooks
# ===========================================================================

def _live_ircx_adapter() -> Optional["IRCXAdapter"]:
    """Fetch the running IRCXAdapter from the in-process gateway, if any."""
    try:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
        if runner is None:
            return None
        return runner.adapters.get(Platform("ircx"))
    except Exception:
        return None


def _ircx_join_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    channel = str(args.get("channel", "")).strip()
    key = args.get("key") or None
    res = _run_adapter_coro(adapter, adapter.runtime_join(channel, key))
    return json.dumps(res)


def _ircx_part_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    res = _run_adapter_coro(adapter, adapter.runtime_part(str(args.get("channel", "")).strip(), args.get("reason")))
    return json.dumps(res)


def _ircx_say_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    res = _run_adapter_coro(adapter, adapter.runtime_say(str(args.get("target", "")).strip(), str(args.get("text", ""))))
    return json.dumps(res)


def _ircx_list_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    return json.dumps(adapter.runtime_list())


def _ircx_channel_info_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    res = _run_adapter_coro(adapter, adapter.runtime_channel_info(str(args.get("channel", "")).strip()))
    return json.dumps(res)


def _ircx_whois_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    res = _run_adapter_coro(adapter, adapter.runtime_whois(str(args.get("nick", "")).strip()))
    return json.dumps(res)


def _ircx_topic_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    topic = args.get("topic")
    res = _run_adapter_coro(adapter, adapter.runtime_topic(
        str(args.get("channel", "")).strip(),
        None if topic is None else str(topic)))
    return json.dumps(res)


def _ircx_notice_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    res = _run_adapter_coro(adapter, adapter.runtime_notice(
        str(args.get("target", "")).strip(), str(args.get("text", ""))))
    return json.dumps(res)


def _ircx_nick_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    res = _run_adapter_coro(adapter, adapter.runtime_nick(str(args.get("nick", "")).strip()))
    return json.dumps(res)


def _ircx_mode_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    mode = args.get("mode")
    margs = args.get("args") or []
    if isinstance(margs, str):
        margs = margs.split()
    res = _run_adapter_coro(adapter, adapter.runtime_mode(
        str(args.get("channel", "")).strip(),
        None if mode is None else str(mode),
        [str(a) for a in margs]))
    return json.dumps(res)


def _ircx_kick_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    res = _run_adapter_coro(adapter, adapter.runtime_kick(
        str(args.get("channel", "")).strip(),
        str(args.get("nick", "")).strip(),
        args.get("reason")))
    return json.dumps(res)


def _ircx_accounts_online_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    res = _run_adapter_coro(adapter, adapter.runtime_accounts_online())
    return json.dumps(res)


def _ircx_search_history_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    res = _run_adapter_coro(adapter, adapter.runtime_search_history(
        str(args.get("query", "")),
        str(args.get("channel", "")).strip(),
        str(args.get("sender", "")).strip(),
        args.get("limit", 20)))
    return json.dumps(res)


def _ircx_away_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    message = args.get("message")
    res = _run_adapter_coro(adapter, adapter.runtime_away(
        None if message is None else str(message)))
    return json.dumps(res)


def _ircx_cycle_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    res = _run_adapter_coro(adapter, adapter.runtime_cycle(
        str(args.get("channel", "")).strip(), args.get("reason")))
    return json.dumps(res)


def _ircx_set_key_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    key = args.get("key")
    res = _run_adapter_coro(adapter, adapter.runtime_set_key(
        str(args.get("channel", "")).strip(),
        None if key is None else str(key)))
    return json.dumps(res)


def _ircx_whois_server_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    res = _run_adapter_coro(adapter, adapter.runtime_whois_server(
        str(args.get("nick", "")).strip()))
    return json.dumps(res)


def _ircx_ignore_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    res = _run_adapter_coro(adapter, adapter.runtime_ignore(
        str(args.get("nick", "")).strip(), args.get("seconds", 300)))
    return json.dumps(res)


def _ircx_unignore_tool(args: dict, **kwargs) -> str:
    import json
    adapter = _live_ircx_adapter()
    if adapter is None:
        return json.dumps({"error": "IRC not connected in this process"})
    res = _run_adapter_coro(adapter, adapter.runtime_unignore(
        str(args.get("nick", "")).strip()))
    return json.dumps(res)


def _run_adapter_coro(adapter, coro, timeout: float = 30.0):
    """Run an adapter coroutine from a tool handler, on the *gateway* loop.

    Hermes dispatches tools inside a ThreadPoolExecutor worker thread (see
    agent/tool_executor.py), so there is normally no running loop in this
    thread. The coroutine touches the client's send queue and StreamWriter,
    which are bound to the gateway's event loop — running it on any *other*
    loop (e.g. a throwaway ``asyncio.run`` loop) is cross-loop-unsafe. So we
    schedule it onto the loop the client captured at connect() time and block
    this worker thread for the result.

    This cannot deadlock: we run on a worker thread, never the loop thread,
    so blocking here does not stop the loop from ticking. If, unexpectedly,
    we ARE on the gateway loop thread, ``run_coroutine_threadsafe`` would
    deadlock — so we detect that and fall back to a fresh loop instead.
    """
    client = getattr(adapter, "_client", None)
    loop = getattr(client, "_loop", None) if client is not None else None

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if loop is not None and loop.is_running() and running is not loop:
        # Normal path: worker thread → schedule onto the gateway loop.
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)

    # No live gateway loop (e.g. tests), or we're somehow on the loop thread:
    # run on a private loop. Safe because nothing here shares loop-bound state
    # in that scenario.
    return asyncio.run(coro)


_IRCX_TOOL_SCHEMAS = {
    "irc_join": {
        "name": "irc_join",
        "description": (
            "Join an IRC channel at runtime (requires operator opt-in via "
            "IRCX_ALLOW_AGENT_JOIN). The bot stays in the channel and rejoins "
            "after reconnects."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel, e.g. #help"},
                "key": {"type": "string", "description": "Optional channel key/password"},
            },
            "required": ["channel"],
        },
    },
    "irc_part": {
        "name": "irc_part",
        "description": "Leave an IRC channel the bot has joined (requires IRCX_ALLOW_AGENT_JOIN).",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "reason": {"type": "string", "description": "Optional part message"},
            },
            "required": ["channel"],
        },
    },
    "irc_say": {
        "name": "irc_say",
        "description": (
            "Send a message to an IRC channel the bot is in, or a direct "
            "message to a nick. Use to proactively speak in a specific channel."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Channel (#x) or nick"},
                "text": {"type": "string"},
            },
            "required": ["target", "text"],
        },
    },
    "irc_list_channels": {
        "name": "irc_list_channels",
        "description": "List the IRC channels the bot is currently in, and its nick.",
        "parameters": {"type": "object", "properties": {}},
    },
    "irc_channel_info": {
        "name": "irc_channel_info",
        "description": (
            "Get the live roster of a channel the bot is in: the member list "
            "(with @ for ops and + for voiced), total user count, who the ops "
            "and voiced users are, and the channel topic. Use this to answer "
            "'who is here', 'how many users/ops', 'what's the topic', etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel name, e.g. #help. Defaults to the bot's primary channel."},
            },
        },
    },
    "irc_whois": {
        "name": "irc_whois",
        "description": (
            "Look up what the bot knows about a specific IRC user it shares a "
            "channel with: their nick, ident/host, verified account (if any), "
            "away status, and which shared channels they're in."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "nick": {"type": "string", "description": "The nickname to look up."},
            },
            "required": ["nick"],
        },
    },
    "irc_topic": {
        "name": "irc_topic",
        "description": (
            "Read or set a channel's topic. Omit 'topic' to read the current "
            "topic; provide it to change the topic (the server may require the "
            "bot to be a channel operator)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel; defaults to the primary channel."},
                "topic": {"type": "string", "description": "New topic. Omit to just read."},
            },
        },
    },
    "irc_notice": {
        "name": "irc_notice",
        "description": (
            "Send an IRC NOTICE (instead of a normal message) to a channel the "
            "bot is in, or to a nick. NOTICEs are the convention for automated "
            "/ non-conversational bot output and suppress client auto-replies."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Channel (#x) or nick."},
                "text": {"type": "string"},
            },
            "required": ["target", "text"],
        },
    },
    "irc_nick": {
        "name": "irc_nick",
        "description": (
            "Change the bot's own nickname at runtime. The server confirms the "
            "change asynchronously; note the bot's keepalive will try to reclaim "
            "its configured nick over time."
        ),
        "parameters": {
            "type": "object",
            "properties": {"nick": {"type": "string"}},
            "required": ["nick"],
        },
    },
    "irc_mode": {
        "name": "irc_mode",
        "description": (
            "Read or change channel modes. Omit 'mode' to read current modes "
            "(answers 'what's the mode on #channel?'); provide e.g. '+m' or "
            "'-t' to change them (requires the bot to be a channel operator)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel; defaults to the primary channel."},
                "mode": {"type": "string", "description": "Mode change like '+m', '-t', '+o nick'. Omit to read."},
                "args": {"type": "string", "description": "Optional space-separated mode arguments."},
            },
        },
    },
    "irc_kick": {
        "name": "irc_kick",
        "description": (
            "Remove a user from a channel. Disabled unless the operator sets "
            "IRCX_ALLOW_AGENT_KICK=true, and only works when the bot itself "
            "holds operator status. Use sparingly and only when an op asks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "nick": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["channel", "nick"],
        },
    },
    "irc_accounts_online": {
        "name": "irc_accounts_online",
        "description": (
            "List which of the bot's allowed users (by verified account or "
            "nick) are currently visible/connected in shared channels. Answers "
            "'who from my people is around right now?'."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    "irc_search_history": {
        "name": "irc_search_history",
        "description": (
            "Search the bot's logged channel history by keyword, sender, and "
            "channel (requires IRCX_LOG_DIR logging). Answers 'what did X say "
            "about Y earlier?'. Returns matching lines with timestamps."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword/substring to match (case-insensitive)."},
                "channel": {"type": "string", "description": "Channel to search; defaults to the primary channel."},
                "sender": {"type": "string", "description": "Restrict to a specific sender nick."},
                "limit": {"type": "integer", "description": "Max results (default 20, cap 100)."},
            },
            "required": ["query"],
        },
    },
    "irc_away": {
        "name": "irc_away",
        "description": (
            "Set or clear the bot's IRC away status. Provide 'message' to mark "
            "away (signals you're stepping back or degraded — clients show it on "
            "WHOIS and when someone messages you); omit/empty 'message' to return."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Away reason. Omit or empty to clear away."},
            },
        },
    },
    "irc_cycle": {
        "name": "irc_cycle",
        "description": (
            "Part then rejoin a channel to reset desynced state (e.g. after a "
            "netsplit or lost op status). The channel key is preserved across "
            "the cycle. The bot must currently be in the channel."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "reason": {"type": "string", "description": "Optional part reason."},
            },
            "required": ["channel"],
        },
    },
    "irc_set_key": {
        "name": "irc_set_key",
        "description": (
            "Set or clear a channel key (+k, password to join). Provide 'key' to "
            "set it; omit/empty 'key' to remove it. Requires the bot to hold "
            "operator status. The key is remembered so reconnects rejoin with it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "key": {"type": "string", "description": "New key. Omit or empty to clear (-k)."},
            },
            "required": ["channel"],
        },
    },
    "irc_whois_server": {
        "name": "irc_whois_server",
        "description": (
            "Network-wide WHOIS lookup of a nick — works even for users the bot "
            "does NOT share a channel with. Answers 'is X online right now?' and "
            "returns their account, host, realname, server, idle time and "
            "channels. Returns an error if the nick is offline."
        ),
        "parameters": {
            "type": "object",
            "properties": {"nick": {"type": "string"}},
            "required": ["nick"],
        },
    },
    "irc_ignore": {
        "name": "irc_ignore",
        "description": (
            "Temporarily mute a user: their messages are dropped (not answered, "
            "not added to context) for a while, then ignoring lapses on its own "
            "so it doesn't become permanent. Use to let someone cool off."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "nick": {"type": "string"},
                "seconds": {"type": "integer", "description": "How long to ignore (default 300, max 86400)."},
            },
            "required": ["nick"],
        },
    },
    "irc_unignore": {
        "name": "irc_unignore",
        "description": "Lift a temporary mute set by irc_ignore before it expires.",
        "parameters": {
            "type": "object",
            "properties": {"nick": {"type": "string"}},
            "required": ["nick"],
        },
    },
}

def check_requirements() -> bool:
    """Dependencies present and minimally configured?"""
    if not _LIBS_OK:
        return False
    server = _env("IRCX_SERVER", "IRC_SERVER")
    channel = _env("IRCX_CHANNEL", "IRCX_CHANNELS", "IRC_CHANNEL", "IRC_CHANNELS")
    return bool(server and channel)


def validate_config(config: Any) -> bool:
    if not _LIBS_OK:
        return False
    cfg = load_config(config)
    return bool(cfg.server and cfg.channels)


def is_connected(config: Any) -> bool:
    extra = getattr(config, "extra", {}) or {}
    server = _env("IRCX_SERVER", "IRC_SERVER") or extra.get("server")
    channel = (
        _env("IRCX_CHANNEL", "IRCX_CHANNELS", "IRC_CHANNEL", "IRC_CHANNELS")
        or extra.get("channels") or extra.get("channel")
    )
    return bool(server and channel)


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env vars before adapter construction."""
    server = _env("IRCX_SERVER", "IRC_SERVER")
    channel = _env("IRCX_CHANNEL", "IRCX_CHANNELS", "IRC_CHANNEL", "IRC_CHANNELS")
    if not (server and channel):
        return None
    seed: dict = {"server": server, "channel": channel}
    port = _env("IRCX_PORT", "IRC_PORT")
    if port:
        try:
            seed["port"] = int(port)
        except ValueError:
            pass
    nickname = _env("IRCX_NICKNAME", "IRC_NICKNAME")
    if nickname:
        seed["nickname"] = nickname
    use_tls = _env("IRCX_USE_TLS", "IRC_USE_TLS", "IRC_TLS")
    if use_tls is not None:
        seed["use_tls"] = _truthy(use_tls)
    home = _env("IRCX_HOME_CHANNEL", "IRC_HOME_CHANNEL") or channel.split(",")[0].strip()
    if home:
        seed["home_channel"] = {"chat_id": home, "name": home}
    return seed


async def _standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process delivery for cron jobs (no live gateway adapter).

    Opens an ephemeral connection with a ``-cron`` nick suffix, joins the
    target channel, sends, and quits.
    """
    if not _LIBS_OK:
        return {"error": f"IRCX standalone send: {INSTALL_HINT}"}
    cfg = load_config(pconfig)
    if not cfg.server:
        return {"error": "IRCX standalone send: server not configured"}
    target = chat_id or (cfg.home_channel or "")
    if not target:
        return {"error": "IRCX standalone send: no target"}
    if any(ch in target for ch in ("\r", "\n", "\x00", " ")):
        return {"error": "IRCX standalone send: illegal characters in target"}

    # Only join + send to channels; DMs go straight to the nick.
    join_target = target if (target[:1] in "#&+!") else None
    # Make the cron client a configured channel so allowlist JOIN works.
    if join_target and not any(c.name.lower() == join_target.lower() for c in cfg.channels):
        cfg.channels.append(ChannelSpec(name=join_target))

    client = IRCClient(cfg, on_message=_noop_message, nick_suffix="-cron")
    try:
        await client.connect(timeout=20.0)
    except Exception as exc:
        try:
            await client.disconnect()
        except Exception:
            pass
        return {"error": f"IRCX standalone connect failed: {exc}"}

    try:
        for line in split_message(message, target, cfg.max_message_length, convert_formatting=cfg.convert_formatting):
            client.privmsg(target, line)
        await asyncio.sleep(0.5 * max(1, len(message) // 400 + 1))
        return {"success": True, "message_id": str(int(time.time() * 1000))}
    except Exception as exc:
        return {"error": f"IRCX standalone send failed: {exc}"}
    finally:
        await client.disconnect("delivered")


async def _noop_message(_msg: Dict[str, Any]) -> None:
    return None


def interactive_setup() -> None:
    """Interactive ``hermes gateway setup`` flow for IRCX."""
    from hermes_cli.setup import (
        prompt, prompt_yes_no, save_env_value, get_env_value,
        print_header, print_info, print_warning, print_success,
    )

    print_header("IRC (IRCX)")
    if not _LIBS_OK:
        print_warning(f"IRCv3 libraries missing. Run: {INSTALL_HINT}")
    existing = get_env_value("IRCX_SERVER") or get_env_value("IRC_SERVER")
    if existing:
        print_info(f"IRCX: already configured (server: {existing})")
        if not prompt_yes_no("Reconfigure IRCX?", False):
            return

    print_info("IRCv3 gateway. SASL-capable, multi-channel, account-verified auth.")
    server = prompt("IRC server hostname (e.g. irc.libera.chat)", default=existing or "")
    if not server:
        print_warning("Server is required — skipping IRCX setup")
        return
    save_env_value("IRCX_SERVER", server.strip())

    use_tls = prompt_yes_no("Use TLS (recommended)?", True)
    save_env_value("IRCX_USE_TLS", "true" if use_tls else "false")
    default_port = "6697" if use_tls else "6667"
    port = prompt(f"Port (default {default_port})", default=get_env_value("IRCX_PORT") or "")
    if port:
        try:
            save_env_value("IRCX_PORT", str(int(port)))
        except ValueError:
            print_warning(f"Invalid port — using default {default_port}")

    nickname = prompt("Bot nickname (e.g. hermes-bot)", default=get_env_value("IRCX_NICKNAME") or "")
    if not nickname:
        print_warning("Nickname is required — skipping IRCX setup")
        return
    save_env_value("IRCX_NICKNAME", nickname.strip())

    channel = prompt("Channel(s) to join (comma-separated, '#chan key' for keyed)",
                     default=get_env_value("IRCX_CHANNEL") or "")
    if not channel:
        print_warning("Channel is required — skipping IRCX setup")
        return
    save_env_value("IRCX_CHANNEL", channel.strip())

    print()
    print_info("🔑 Authentication (SASL preferred over NickServ)")
    if prompt_yes_no("Configure SASL?", False):
        mech = prompt("Mechanism (PLAIN/EXTERNAL/SCRAM-SHA-256)", default="PLAIN") or "PLAIN"
        save_env_value("IRCX_SASL_MECHANISM", mech.strip().upper())
        if mech.strip().upper() != "EXTERNAL":
            sasl_user = prompt("SASL username (account)", default=nickname.strip())
            if sasl_user:
                save_env_value("IRCX_SASL_USERNAME", sasl_user.strip())
            sasl_pw = prompt("SASL password", password=True)
            if sasl_pw:
                save_env_value("IRCX_SASL_PASSWORD", sasl_pw)
    elif prompt_yes_no("Identify with NickServ on connect?", False):
        ns = prompt("NickServ password", password=True)
        if ns:
            save_env_value("IRCX_NICKSERV_PASSWORD", ns)

    print()
    print_info("🔒 Access control")
    print_info("   By default only network-verified accounts are authorized.")
    if prompt_yes_no("Allow all users (dev only)?", False):
        save_env_value("IRCX_ALLOW_ALL_USERS", "true")
        print_warning("⚠️  Open access — anyone may command the bot.")
    else:
        save_env_value("IRCX_ALLOW_ALL_USERS", "false")
        allowed = prompt("Allowed accounts/nicks (comma-separated)",
                         default=get_env_value("IRCX_ALLOWED_USERS") or "")
        save_env_value("IRCX_ALLOWED_USERS", allowed.replace(" ", "") if allowed else "")

    print()
    print_success("IRCX configuration saved to ~/.hermes/.env")
    print_info("Restart the gateway: hermes gateway restart")


def register(ctx: Any) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="ircx",
        label="IRC",
        adapter_factory=lambda cfg: IRCXAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["IRCX_SERVER", "IRCX_CHANNEL", "IRCX_NICKNAME"],
        install_hint=INSTALL_HINT,
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="IRCX_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="IRCX_ALLOWED_USERS",
        allow_all_env="IRCX_ALLOW_ALL_USERS",
        max_message_length=450,
        emoji="💬",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via IRC. IRC does not support markdown rendering "
            "— use plain text. Long messages are automatically split into "
            "~450-character lines. In channels users address you by prefixing "
            "your nick. Keep responses concise and conversational. You have "
            "irc_join / irc_part / irc_say / irc_list_channels tools to manage "
            "channels when permitted (only join channels when explicitly asked), "
            "and irc_channel_info / irc_whois to see who is in a channel, the "
            "user/op counts, the topic, and details about a specific user. "
            "You can also irc_away to flag stepping back, irc_whois_server to "
            "check if a nick is online network-wide, irc_cycle to reset a "
            "desynced channel, irc_set_key to manage a channel password, and "
            "irc_ignore to briefly mute someone (it lapses on its own)."
        ),
    )

    # Runtime agency tools (Feature B).  Registered under the ``ircx`` toolset;
    # enable it for the platform (platform_toolsets.ircx) to expose them.
    _register = getattr(ctx, "register_tool", None)
    if callable(_register):
        for _name, _handler in (
            ("irc_join", _ircx_join_tool),
            ("irc_part", _ircx_part_tool),
            ("irc_say", _ircx_say_tool),
            ("irc_list_channels", _ircx_list_tool),
            ("irc_channel_info", _ircx_channel_info_tool),
            ("irc_whois", _ircx_whois_tool),
            ("irc_topic", _ircx_topic_tool),
            ("irc_notice", _ircx_notice_tool),
            ("irc_nick", _ircx_nick_tool),
            ("irc_mode", _ircx_mode_tool),
            ("irc_kick", _ircx_kick_tool),
            ("irc_accounts_online", _ircx_accounts_online_tool),
            ("irc_search_history", _ircx_search_history_tool),
            ("irc_away", _ircx_away_tool),
            ("irc_cycle", _ircx_cycle_tool),
            ("irc_set_key", _ircx_set_key_tool),
            ("irc_whois_server", _ircx_whois_server_tool),
            ("irc_ignore", _ircx_ignore_tool),
            ("irc_unignore", _ircx_unignore_tool),
        ):
            try:
                _register(
                    name=_name,
                    toolset="ircx",
                    schema=_IRCX_TOOL_SCHEMAS[_name],
                    handler=_handler,
                    description=_IRCX_TOOL_SCHEMAS[_name]["description"],
                    emoji="💬",
                )
            except Exception as exc:  # pragma: no cover
                logger.debug("IRCX: could not register tool %s: %s", _name, exc)

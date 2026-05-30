"""Tests for the IRCX production IRCv3 platform adapter plugin.

These tests are network-free: the IRC client engine is driven by feeding it
raw protocol lines and inspecting the outbound send queue, so the full
CAP/SASL/registration state machine, message gating, authorization and
formatting paths are all exercised deterministically.
"""

import asyncio
import base64
import hashlib
import hmac
import os

import pytest
from unittest.mock import AsyncMock

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

ircx = load_plugin_adapter("ircx")

IRCXConfig = ircx.IRCXConfig
ChannelSpec = ircx.ChannelSpec
IRCClient = ircx.IRCClient
IRCXAdapter = ircx.IRCXAdapter


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _clear_irc_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("IRC"):
            monkeypatch.delenv(key, raising=False)


def make_client(monkeypatch=None, **cfg_overrides):
    """Create an IRCClient with a captured on_message sink (no socket)."""
    if monkeypatch is not None:
        _clear_irc_env(monkeypatch)
    base = dict(server="irc.test", nickname="hermesx", channels=[ChannelSpec("#chan")])
    base.update(cfg_overrides)
    cfg = IRCXConfig(**base)
    received = []

    async def on_msg(d):
        received.append(d)

    client = IRCClient(cfg, on_message=on_msg)
    return client, received


async def feed(client, *raw_lines):
    """Feed raw protocol lines through the state machine + dispatcher."""
    for raw in raw_lines:
        for line in client.server.recv((raw + "\r\n").encode("utf-8")):
            client.server.parse_tokens(line)
            await client._handle_line(line)


def drain(client):
    """Pop all queued outbound lines as decoded strings."""
    out = []
    while not client._send_q.empty():
        data = client._send_q.get_nowait()
        if data is None:
            continue
        out.append(data.decode("utf-8").rstrip("\r\n"))
    return out


# ===========================================================================
# Config parsing
# ===========================================================================

class TestConfig:
    def test_env_overrides_extra(self, monkeypatch):
        _clear_irc_env(monkeypatch)
        monkeypatch.setenv("IRCX_SERVER", "env.server")
        monkeypatch.setenv("IRCX_PORT", "6667")
        monkeypatch.setenv("IRCX_USE_TLS", "false")
        from gateway.config import PlatformConfig
        cfg = ircx.load_config(PlatformConfig(extra={"server": "yaml.server", "port": 1234, "channel": "#x"}))
        assert cfg.server == "env.server"
        assert cfg.port == 6667
        assert cfg.use_tls is False

    def test_legacy_irc_env_fallback(self, monkeypatch):
        _clear_irc_env(monkeypatch)
        monkeypatch.setenv("IRC_SERVER", "legacy.server")
        monkeypatch.setenv("IRC_CHANNEL", "#legacy")
        monkeypatch.setenv("IRC_NICKNAME", "legacybot")
        from gateway.config import PlatformConfig
        cfg = ircx.load_config(PlatformConfig())
        assert cfg.server == "legacy.server"
        assert cfg.channel_names() == ["#legacy"]
        assert cfg.nickname == "legacybot"

    def test_default_port_follows_tls(self, monkeypatch):
        _clear_irc_env(monkeypatch)
        from gateway.config import PlatformConfig
        cfg = ircx.load_config(PlatformConfig(extra={"server": "s", "channel": "#c", "use_tls": False}))
        assert cfg.port == 6667
        cfg2 = ircx.load_config(PlatformConfig(extra={"server": "s", "channel": "#c", "use_tls": True}))
        assert cfg2.port == 6697

    def test_channels_string_with_keys(self, monkeypatch):
        _clear_irc_env(monkeypatch)
        monkeypatch.setenv("IRCX_SERVER", "s")
        monkeypatch.setenv("IRCX_CHANNEL", "#open, #ops secret, #dev")
        from gateway.config import PlatformConfig
        cfg = ircx.load_config(PlatformConfig())
        names = [(c.name, c.key) for c in cfg.channels]
        assert names == [("#open", None), ("#ops", "secret"), ("#dev", None)]

    def test_groups_overrides(self, monkeypatch):
        _clear_irc_env(monkeypatch)
        from gateway.config import PlatformConfig
        cfg = ircx.load_config(PlatformConfig(extra={
            "server": "s", "channels": ["#a", "#b"],
            "groups": {
                "#b": {"require_mention": False, "allow_from": ["alice", "bob"],
                       "tools": ["read_file"], "tools_by_sender": {"alice": ["*"]}},
                "#c": {"require_mention": True},  # not in channels -> appended
            },
        }))
        b = [c for c in cfg.channels if c.name == "#b"][0]
        assert b.require_mention is False
        assert b.allow_from == ["alice", "bob"]
        assert b.tools == ["read_file"]
        assert b.tools_by_sender == {"alice": ["*"]}
        assert any(c.name == "#c" for c in cfg.channels)

    def test_home_channel_defaults_to_first(self, monkeypatch):
        _clear_irc_env(monkeypatch)
        from gateway.config import PlatformConfig
        cfg = ircx.load_config(PlatformConfig(extra={"server": "s", "channels": ["#first", "#second"]}))
        assert cfg.home_channel == "#first"

    def test_group_policy_validation(self, monkeypatch):
        _clear_irc_env(monkeypatch)
        monkeypatch.setenv("IRCX_GROUP_POLICY", "bogus")
        from gateway.config import PlatformConfig
        cfg = ircx.load_config(PlatformConfig(extra={"server": "s", "channel": "#c"}))
        assert cfg.group_policy == "allowlist"


# ===========================================================================
# Formatting / splitting
# ===========================================================================

class TestFormatting:
    def test_strip_markdown(self):
        assert ircx.strip_markdown("**bold** _italic_ `code`") == "bold italic code"
        assert ircx.strip_markdown("[text](http://u)") == "text (http://u)"
        assert ircx.strip_markdown("![alt](http://img)") == "http://img"

    def test_markdown_to_irc_codes(self):
        out = ircx.markdown_to_irc("**b** *i*")
        assert "\x02b\x02" in out
        assert "\x1di\x1d" in out

    def test_control_char_stripping(self):
        assert ircx.strip_irc_control_chars("a\r\nb\x00c") == "a  bc"

    def test_split_respects_byte_limit(self):
        text = "x" * 2000
        lines = ircx.split_message(text, "#chan", 450)
        assert all(len(l.encode()) <= 450 for l in lines)
        assert "".join(lines) == text

    def test_split_protocol_limit_caps_user_limit(self):
        # With a generous user limit, protocol overhead still caps the line.
        text = "y" * 2000
        lines = ircx.split_message(text, "#averylongchannelname", 100000)
        assert all(len(f"PRIVMSG #averylongchannelname :{l}".encode()) <= 510 for l in lines)

    def test_split_unicode_boundary(self):
        text = "€" * 400  # 3 bytes each
        lines = ircx.split_message(text, "#c", 60)
        assert all(len(l.encode()) <= 60 for l in lines)
        assert "".join(lines) == text

    def test_split_prefers_spaces(self):
        text = "word " * 40
        lines = ircx.split_message(text, "#c", 60)
        # No line should start mid-word (leading space stripped).
        assert all(not l.startswith(" ") for l in lines)


# ===========================================================================
# SASL
# ===========================================================================

class TestSASL:
    def test_plain_payload(self):
        payload = ircx.sasl_plain_payload("user", "pass")
        assert base64.b64decode(payload) == b"\x00user\x00pass"

    def test_chunk_authenticate_short(self):
        assert ircx._chunk_authenticate("abc") == ["abc"]

    def test_chunk_authenticate_empty(self):
        assert ircx._chunk_authenticate("") == ["+"]

    def test_chunk_authenticate_exact_400(self):
        payload = "a" * 400
        chunks = ircx._chunk_authenticate(payload)
        assert chunks == [payload, "+"]

    def test_chunk_authenticate_long(self):
        payload = "b" * 850
        chunks = ircx._chunk_authenticate(payload)
        assert chunks == ["b" * 400, "b" * 400, "b" * 50]

    def test_scram_hash_mapping(self):
        assert ircx._scram_hash_for("SCRAM-SHA-256") == "sha256"
        assert ircx._scram_hash_for("SCRAM-SHA-512") == "sha512"
        assert ircx._scram_hash_for("PLAIN") is None
        assert ircx._scram_hash_for("EXTERNAL") is None

    @pytest.mark.parametrize("hash_name", ["sha256", "sha512"])
    def test_scram_full_exchange(self, hash_name):
        """Drive a SCRAM client against a reference server impl."""
        username, password = "alice", "hunter2"
        client = ircx.ScramClient(username, password, hash_name)

        client_first = client.client_first().decode()
        assert client_first.startswith("n,,n=alice,r=")
        cnonce = client_first.split(",r=", 1)[1]

        # --- reference server side (RFC 5802) ---
        salt = b"0123456789abcdef"
        iterations = 4096
        snonce = cnonce + "serverpart"
        server_first = f"r={snonce},s={base64.b64encode(salt).decode()},i={iterations}"

        client_final = client.client_final(server_first.encode())
        cf = dict(p.split("=", 1) for p in client_final.decode().split(",", 2) if "=" in p)
        assert cf["r"] == snonce

        salted = hashlib.pbkdf2_hmac(hash_name, password.encode(), salt, iterations)
        client_key = hmac.new(salted, b"Client Key", hash_name).digest()
        stored_key = hashlib.new(hash_name, client_key).digest()
        client_first_bare = client_first[3:]  # strip "n,,"
        client_final_no_proof = client_final.decode().rsplit(",p=", 1)[0]
        auth_message = f"{client_first_bare},{server_first},{client_final_no_proof}"
        expected_sig = hmac.new(stored_key, auth_message.encode(), hash_name).digest()
        expected_proof = bytes(a ^ b for a, b in zip(client_key, expected_sig))
        got_proof = base64.b64decode(client_final.decode().rsplit(",p=", 1)[1])
        assert got_proof == expected_proof  # server verifies the client

        # server-final -> client verifies server signature
        server_key = hmac.new(salted, b"Server Key", hash_name).digest()
        server_sig = hmac.new(server_key, auth_message.encode(), hash_name).digest()
        server_final = f"v={base64.b64encode(server_sig).decode()}"
        assert client.verify_server_final(server_final.encode()) is True

    def test_scram_rejects_bad_nonce(self):
        client = ircx.ScramSha256Client("a", "b")
        client.client_first()
        with pytest.raises(ircx.SASLError):
            client.client_final(b"r=totallywrong,s=AAAA,i=1")


# ===========================================================================
# CAP / SASL negotiation state machine
# ===========================================================================

class TestCapNegotiation:
    @pytest.mark.asyncio
    async def test_cap_ls_requests_offered_caps(self):
        client, _ = make_client()
        client._cap_negotiating = True
        await feed(client, ":srv CAP * LS :message-tags server-time sasl account-tag")
        out = drain(client)
        req = [l for l in out if l.startswith("CAP REQ")]
        assert req, out
        assert "message-tags" in req[0]
        assert "server-time" in req[0]

    @pytest.mark.asyncio
    async def test_cap_ls_multiline(self):
        client, _ = make_client()
        client._cap_negotiating = True
        await feed(client,
                   ":srv CAP * LS * :message-tags server-time",
                   ":srv CAP * LS :sasl echo-message")
        out = drain(client)
        req = [l for l in out if l.startswith("CAP REQ")][0]
        for cap in ("message-tags", "server-time", "sasl", "echo-message"):
            assert cap in req

    @pytest.mark.asyncio
    async def test_cap_ack_without_sasl_ends_negotiation(self):
        client, _ = make_client()
        client._cap_negotiating = True
        await feed(client, ":srv CAP * LS :message-tags server-time")
        drain(client)
        await feed(client, ":srv CAP * ACK :message-tags server-time")
        out = drain(client)
        assert "CAP END" in out
        assert client.has_cap("message-tags")
        assert client.has_cap("server-time")

    @pytest.mark.asyncio
    async def test_sasl_plain_flow(self):
        client, _ = make_client(sasl_mechanism="PLAIN", sasl_username="alice", sasl_password="pw")
        client._cap_negotiating = True
        await feed(client, ":srv CAP * LS :sasl message-tags")
        drain(client)
        await feed(client, ":srv CAP * ACK :sasl message-tags")
        out = drain(client)
        assert "AUTHENTICATE PLAIN" in out
        # CAP END must NOT be sent yet (SASL in progress)
        assert "CAP END" not in out
        await feed(client, "AUTHENTICATE +")
        out = drain(client)
        auth = [l for l in out if l.startswith("AUTHENTICATE ") and l != "AUTHENTICATE PLAIN"]
        assert auth
        assert base64.b64decode(auth[0].split(" ", 1)[1]) == b"\x00alice\x00pw"
        # success numeric ends negotiation
        await feed(client, ":srv 903 hermesx :SASL authentication successful")
        out = drain(client)
        assert "CAP END" in out
        assert client._sasl_ok is True

    @pytest.mark.asyncio
    async def test_sasl_failure_still_ends_cap(self):
        client, _ = make_client(sasl_mechanism="PLAIN", sasl_password="bad")
        client._cap_negotiating = True
        await feed(client, ":srv CAP * LS :sasl")
        drain(client)
        await feed(client, ":srv CAP * ACK :sasl")
        await feed(client, "AUTHENTICATE +")
        drain(client)
        await feed(client, ":srv 904 hermesx :SASL authentication failed")
        out = drain(client)
        assert "CAP END" in out
        assert client._sasl_ok is False

    @pytest.mark.asyncio
    async def test_external_mechanism_sends_plus(self):
        client, _ = make_client(sasl_mechanism="EXTERNAL")
        client._cap_negotiating = True
        await feed(client, ":srv CAP * LS :sasl")
        drain(client)
        await feed(client, ":srv CAP * ACK :sasl")
        out = drain(client)
        assert "AUTHENTICATE EXTERNAL" in out
        await feed(client, "AUTHENTICATE +")
        out = drain(client)
        assert "AUTHENTICATE +" in out


# ===========================================================================
# Protocol behaviours
# ===========================================================================

class TestProtocol:
    @pytest.mark.asyncio
    async def test_ping_pong(self):
        client, _ = make_client()
        await feed(client, "PING :abc123")
        assert "PONG abc123" in drain(client)

    @pytest.mark.asyncio
    async def test_registration_event(self):
        client, _ = make_client()
        await feed(client, ":srv 001 hermesx :Welcome to the network")
        assert client._registered_evt.is_set()

    @pytest.mark.asyncio
    async def test_nick_in_use_retries(self):
        client, _ = make_client()
        await feed(client, ":srv 433 * hermesx :Nickname is already in use")
        out = drain(client)
        assert "NICK hermesx_" in out
        await feed(client, ":srv 433 * hermesx_ :Nickname is already in use")
        out = drain(client)
        assert any(l.startswith("NICK hermesx_") for l in out)

    @pytest.mark.asyncio
    async def test_ctcp_version_reply_not_dispatched(self):
        client, received = make_client()
        await feed(client, ":bob!b@h PRIVMSG hermesx :\x01VERSION\x01")
        out = drain(client)
        assert any(l.startswith("NOTICE bob") and "VERSION" in l for l in out)
        assert received == []  # CTCP is not a user message

    @pytest.mark.asyncio
    async def test_ctcp_action_rendered(self):
        client, received = make_client()
        await feed(client, ":bob!b@h PRIVMSG #chan :\x01ACTION waves\x01")
        assert received
        assert received[0]["text"] == "* bob waves"

    @pytest.mark.asyncio
    async def test_ignore_own_echo(self):
        client, received = make_client()
        await feed(client, ":srv 001 hermesx :hi")  # learn own nick
        await feed(client, ":hermesx!u@h PRIVMSG #chan :hello from me")
        assert received == []

    @pytest.mark.asyncio
    async def test_account_tag_captured(self):
        client, received = make_client()
        await feed(client, "@account=aliceacct :alice!a@h PRIVMSG #chan :hi there")
        assert received[0]["account"] == "aliceacct"

    @pytest.mark.asyncio
    async def test_notice_flagged(self):
        client, received = make_client()
        await feed(client, ":bob!b@h NOTICE #chan :a notice")
        assert received[0]["is_notice"] is True


# ===========================================================================
# Adapter: mention gating, authorization, dispatch
# ===========================================================================

async def make_adapter(monkeypatch, **cfg_overrides):
    _clear_irc_env(monkeypatch)
    from gateway.config import PlatformConfig
    adapter = IRCXAdapter(PlatformConfig(extra={"server": "s", "channels": ["#chan"]}))
    # override resolved config
    base = dict(server="s", nickname="hermesx", channels=[ChannelSpec("#chan")])
    base.update(cfg_overrides)
    adapter.cfg = IRCXConfig(**base)
    client, _ = make_client(**base)
    await feed(client, ":srv 001 hermesx :hi")  # set own nick
    adapter._client = client
    adapter._message_handler = lambda *a, **k: None
    adapter.handle_message = AsyncMock()
    return adapter


def msg(**kw):
    base = dict(sender_nick="alice", account=None, target="#chan", text="hi",
                is_notice=False, is_channel=True, msgid=None, server_time=None, tags={})
    base.update(kw)
    return base


class TestAdapterGating:
    @pytest.mark.asyncio
    async def test_require_mention_drops_unaddressed(self, monkeypatch):
        adapter = await make_adapter(monkeypatch, require_mention=True, dangerously_allow_name_matching=True)
        await adapter._on_irc_message(msg(text="just chatting"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_require_mention_accepts_addressed(self, monkeypatch):
        adapter = await make_adapter(monkeypatch, require_mention=True, dangerously_allow_name_matching=True)
        await adapter._on_irc_message(msg(text="hermesx: hello"))
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.text == "hello"

    @pytest.mark.asyncio
    async def test_require_mention_false_accepts_all(self, monkeypatch):
        adapter = await make_adapter(monkeypatch, require_mention=False, dangerously_allow_name_matching=True)
        await adapter._on_irc_message(msg(text="no mention needed"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_per_channel_require_mention_override(self, monkeypatch):
        adapter = await make_adapter(
            monkeypatch, require_mention=True, dangerously_allow_name_matching=True,
            channels=[ChannelSpec("#chan"), ChannelSpec("#open", require_mention=False)],
        )
        await adapter._on_irc_message(msg(target="#open", text="hi all"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_group_policy_allowlist_drops_unknown_channel(self, monkeypatch):
        adapter = await make_adapter(monkeypatch, require_mention=False,
                                     dangerously_allow_name_matching=True, group_policy="allowlist")
        await adapter._on_irc_message(msg(target="#random", text="hi"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_dm_does_not_require_mention(self, monkeypatch):
        adapter = await make_adapter(monkeypatch, require_mention=True, dangerously_allow_name_matching=True)
        await adapter._on_irc_message(msg(target="hermesx", text="hi", is_channel=False))
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.source.chat_type == "dm"
        assert event.source.chat_id == "alice"


class TestAdapterAuthorization:
    @pytest.mark.asyncio
    async def test_unverified_nick_denied_by_default(self, monkeypatch):
        adapter = await make_adapter(monkeypatch, require_mention=False)
        await adapter._on_irc_message(msg(text="hi", account=None))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_verified_account_allowed(self, monkeypatch):
        adapter = await make_adapter(monkeypatch, require_mention=False)
        await adapter._on_irc_message(msg(text="hi", account="aliceacct"))
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.source.user_id == "aliceacct"
        assert event.source.user_name == "alice"

    @pytest.mark.asyncio
    async def test_dangerous_name_matching_allows_nick(self, monkeypatch):
        adapter = await make_adapter(monkeypatch, require_mention=False, dangerously_allow_name_matching=True)
        await adapter._on_irc_message(msg(text="hi", account=None))
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.source.user_id == "alice"

    @pytest.mark.asyncio
    async def test_channel_allow_from_enforced(self, monkeypatch):
        adapter = await make_adapter(
            monkeypatch, require_mention=False,
            channels=[ChannelSpec("#chan", allow_from=["bob"])],
        )
        await adapter._on_irc_message(msg(text="hi", account="alice"))
        adapter.handle_message.assert_not_called()
        await adapter._on_irc_message(msg(text="hi", account="bob"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_dm_allow_from_enforced(self, monkeypatch):
        adapter = await make_adapter(monkeypatch, require_mention=False, allow_from=["trusted"])
        await adapter._on_irc_message(msg(target="hermesx", is_channel=False, account="alice"))
        adapter.handle_message.assert_not_called()
        await adapter._on_irc_message(msg(target="hermesx", is_channel=False, account="trusted"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_allow_all_env(self, monkeypatch):
        adapter = await make_adapter(monkeypatch, require_mention=False)
        monkeypatch.setenv("IRCX_ALLOW_ALL_USERS", "true")
        await adapter._on_irc_message(msg(text="hi", account=None))
        adapter.handle_message.assert_called_once()


# ===========================================================================
# Adapter: sending
# ===========================================================================

class TestAdapterSend:
    @pytest.mark.asyncio
    async def test_send_splits_and_strips_markdown(self, monkeypatch):
        adapter = await make_adapter(monkeypatch)
        adapter._mark_connected()
        res = await adapter.send("#chan", "**bold** message")
        assert res.success
        out = drain(adapter._client)
        privmsgs = [l for l in out if l.startswith("PRIVMSG #chan")]
        assert privmsgs
        assert "bold message" in privmsgs[0]
        assert "**" not in privmsgs[0]

    @pytest.mark.asyncio
    async def test_send_rejects_bad_chat_id(self, monkeypatch):
        adapter = await make_adapter(monkeypatch)
        adapter._mark_connected()
        res = await adapter.send("#chan with space", "hi")
        assert not res.success

    @pytest.mark.asyncio
    async def test_send_not_connected(self, monkeypatch):
        adapter = await make_adapter(monkeypatch)
        adapter._client = None
        res = await adapter.send("#chan", "hi")
        assert not res.success
        assert res.retryable

    @pytest.mark.asyncio
    async def test_reply_tag_when_message_tags_acked(self, monkeypatch):
        adapter = await make_adapter(monkeypatch)
        adapter._mark_connected()
        adapter._client._cap_acked.add("message-tags")
        await adapter.send("#chan", "hi", reply_to="msg123")
        out = drain(adapter._client)
        assert any("+draft/reply=msg123" in l for l in out)

    @pytest.mark.asyncio
    async def test_typing_only_with_message_tags(self, monkeypatch):
        adapter = await make_adapter(monkeypatch)
        adapter._mark_connected()
        await adapter.send_typing("#chan")  # no message-tags cap
        assert drain(adapter._client) == []
        adapter._client._cap_acked.add("message-tags")
        await adapter.send_typing("#chan")
        out = drain(adapter._client)
        assert any(l.startswith("@+typing=active TAGMSG #chan") for l in out)


# ===========================================================================
# Plugin registration & module hooks
# ===========================================================================

class TestRegistration:
    def test_register_kwargs(self):
        captured = {}

        class Ctx:
            def register_platform(self, **kw):
                captured.update(kw)

        ircx.register(Ctx())
        assert captured["name"] == "ircx"
        assert captured["allowed_users_env"] == "IRCX_ALLOWED_USERS"
        assert captured["allow_all_env"] == "IRCX_ALLOW_ALL_USERS"
        assert captured["cron_deliver_env_var"] == "IRCX_HOME_CHANNEL"
        assert callable(captured["standalone_sender_fn"])
        assert callable(captured["env_enablement_fn"])
        assert captured["max_message_length"] == 450

    def test_check_requirements(self, monkeypatch):
        _clear_irc_env(monkeypatch)
        assert ircx.check_requirements() is False
        monkeypatch.setenv("IRCX_SERVER", "s")
        monkeypatch.setenv("IRCX_CHANNEL", "#c")
        assert ircx.check_requirements() is True

    def test_env_enablement(self, monkeypatch):
        _clear_irc_env(monkeypatch)
        assert ircx._env_enablement() is None
        monkeypatch.setenv("IRCX_SERVER", "s")
        monkeypatch.setenv("IRCX_CHANNEL", "#c,#d")
        monkeypatch.setenv("IRCX_PORT", "6697")
        seed = ircx._env_enablement()
        assert seed["server"] == "s"
        assert seed["channel"] == "#c,#d"
        assert seed["port"] == 6697
        assert seed["home_channel"]["chat_id"] == "#c"

    def test_validate_config(self, monkeypatch):
        _clear_irc_env(monkeypatch)
        from gateway.config import PlatformConfig
        assert ircx.validate_config(PlatformConfig()) is False
        assert ircx.validate_config(PlatformConfig(extra={"server": "s", "channel": "#c"})) is True


class TestStandaloneSend:
    @pytest.mark.asyncio
    async def test_standalone_send_no_server(self, monkeypatch):
        _clear_irc_env(monkeypatch)
        from gateway.config import PlatformConfig
        res = await ircx._standalone_send(PlatformConfig(), "#chan", "hi")
        assert "error" in res

    @pytest.mark.asyncio
    async def test_standalone_send_illegal_target(self, monkeypatch):
        _clear_irc_env(monkeypatch)
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(extra={"server": "s", "channel": "#c"})
        res = await ircx._standalone_send(cfg, "bad target", "hi")
        assert "error" in res


def _core_patch_present() -> bool:
    """True when CORE_PATCH.md (MessageEvent.tool_scope + _apply_tool_scope) is applied."""
    try:
        import dataclasses
        import gateway.run as _gr
        from gateway.platforms.base import MessageEvent as _ME
        return hasattr(_gr, "_apply_tool_scope") and "tool_scope" in {f.name for f in dataclasses.fields(_ME)}
    except Exception:
        return False


_CORE_PATCH = _core_patch_present()


@pytest.mark.skipif(not _CORE_PATCH, reason="requires CORE_PATCH.md (tool_scope hook) applied to gateway")
class TestToolScopeCoreHelper:
    """Exercise the generic gateway/run.py _apply_tool_scope hook."""

    def test_apply_tool_scope_intersects(self):
        from gateway.run import _apply_tool_scope
        assert _apply_tool_scope(["a", "b", "memory"], ["a", "memory"]) == ["a", "memory"]

    def test_apply_tool_scope_wildcard_unrestricted(self):
        from gateway.run import _apply_tool_scope
        assert _apply_tool_scope(["a", "b"], ["*"]) == ["a", "b"]

    def test_apply_tool_scope_none_unrestricted(self):
        from gateway.run import _apply_tool_scope
        assert _apply_tool_scope(["a", "b"], None) == ["a", "b"]

    def test_apply_tool_scope_preserves_order(self):
        from gateway.run import _apply_tool_scope
        assert _apply_tool_scope(["x", "y", "z"], ["z", "x"]) == ["x", "z"]

    def test_messageevent_has_tool_scope_field(self):
        import dataclasses
        from gateway.platforms.base import MessageEvent
        names = {f.name for f in dataclasses.fields(MessageEvent)}
        assert "tool_scope" in names


class TestAdapterToolScope:
    """The adapter attaches the right tool_scope to each MessageEvent."""

    @pytest.mark.asyncio
    async def test_channel_tools_scope(self, monkeypatch):
        adapter = await make_adapter(
            monkeypatch, require_mention=False, dangerously_allow_name_matching=True,
            channels=[ChannelSpec("#chan", tools=["web", "memory"])],
        )
        await adapter._on_irc_message(msg(text="hi"))
        event = adapter.handle_message.call_args[0][0]
        assert event.tool_scope == ["web", "memory"]

    @pytest.mark.asyncio
    async def test_tools_by_sender_overrides_channel(self, monkeypatch):
        adapter = await make_adapter(
            monkeypatch, require_mention=False,
            channels=[ChannelSpec("#chan", tools=["web"],
                                  tools_by_sender={"Alice": ["hermes-cli"]})],
        )
        # identity comes from the verified account; match is case-insensitive
        await adapter._on_irc_message(msg(text="hi", account="alice"))
        event = adapter.handle_message.call_args[0][0]
        assert event.tool_scope == ["hermes-cli"]

    @pytest.mark.asyncio
    async def test_tools_by_sender_falls_back_to_channel(self, monkeypatch):
        adapter = await make_adapter(
            monkeypatch, require_mention=False,
            channels=[ChannelSpec("#chan", tools=["web"],
                                  tools_by_sender={"bob": ["hermes-cli"]})],
        )
        await adapter._on_irc_message(msg(text="hi", account="alice"))
        event = adapter.handle_message.call_args[0][0]
        assert event.tool_scope == ["web"]  # alice not in tools_by_sender

    @pytest.mark.asyncio
    async def test_wildcard_scope(self, monkeypatch):
        adapter = await make_adapter(
            monkeypatch, require_mention=False, dangerously_allow_name_matching=True,
            channels=[ChannelSpec("#chan", tools=["*"])],
        )
        await adapter._on_irc_message(msg(text="hi"))
        event = adapter.handle_message.call_args[0][0]
        assert event.tool_scope == ["*"]

    @pytest.mark.asyncio
    async def test_no_tools_means_no_scope(self, monkeypatch):
        adapter = await make_adapter(monkeypatch, require_mention=False,
                                     dangerously_allow_name_matching=True)
        await adapter._on_irc_message(msg(text="hi"))
        event = adapter.handle_message.call_args[0][0]
        assert event.tool_scope is None

    @pytest.mark.asyncio
    async def test_dm_has_no_scope(self, monkeypatch):
        adapter = await make_adapter(
            monkeypatch, require_mention=False, dangerously_allow_name_matching=True,
            channels=[ChannelSpec("#chan", tools=["web"])],
        )
        await adapter._on_irc_message(msg(target="hermesx", is_channel=False, text="hi"))
        event = adapter.handle_message.call_args[0][0]
        assert event.tool_scope is None

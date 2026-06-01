"""Tests for IRCX advanced features: observe-mode spontaneous replies,
runtime channel-agency tools, and chathistory/logging persistence."""

import os
import pytest
from unittest.mock import AsyncMock

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

ircx = load_plugin_adapter("ircx")
IRCXConfig = ircx.IRCXConfig
ChannelSpec = ircx.ChannelSpec
IRCClient = ircx.IRCClient
IRCXAdapter = ircx.IRCXAdapter


def _clear_irc_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("IRC"):
            monkeypatch.delenv(key, raising=False)


def make_client(**over):
    base = dict(server="irc.test", nickname="hermesx", channels=[ChannelSpec("#chan")])
    base.update(over)
    cfg = IRCXConfig(**base)
    received = []

    async def on_msg(d):
        received.append(d)

    return IRCClient(cfg, on_message=on_msg), received


async def feed(client, *lines):
    for raw in lines:
        for line in client.server.recv((raw + "\r\n").encode()):
            client.server.parse_tokens(line)
            await client._handle_line(line)


def drain(client):
    out = []
    while not client._send_q.empty():
        d = client._send_q.get_nowait()
        if d is not None:
            out.append(d.decode().rstrip("\r\n"))
    return out


async def make_adapter(monkeypatch, **over):
    _clear_irc_env(monkeypatch)
    from gateway.config import PlatformConfig
    adapter = IRCXAdapter(PlatformConfig(extra={"server": "s", "channels": ["#chan"]}))
    base = dict(server="s", nickname="hermesx", channels=[ChannelSpec("#chan")])
    base.update(over)
    adapter.cfg = IRCXConfig(**base)
    client, _ = make_client(**base)
    await feed(client, ":srv 001 hermesx :hi")
    adapter._client = client
    # Route inbound client messages into the adapter so feed() exercises the
    # full client -> adapter path (used by the chathistory-batch test).
    client._on_message = adapter._on_irc_message
    adapter._message_handler = lambda *a, **k: None
    adapter.handle_message = AsyncMock()
    return adapter, client


def cmsg(**kw):
    base = dict(sender_nick="alice", account="alice", target="#chan", text="hi",
                is_notice=False, is_channel=True, msgid=None, server_time=None,
                is_history=False, tags={})
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Feature A: observe mode / spontaneous chime-in
# ---------------------------------------------------------------------------

class TestObserveMode:
    @pytest.mark.asyncio
    async def test_unaddressed_buffered_not_dispatched(self, monkeypatch):
        adapter, _ = await make_adapter(monkeypatch, require_mention=True, observe_mode=True,
                                        spontaneous_probability=0.0)
        await adapter._on_irc_message(cmsg(text="random chatter"))
        adapter.handle_message.assert_not_called()
        # but it was recorded into the rolling buffer
        buf = adapter._buf("#chan")
        assert any("random chatter" in line for line in buf)

    @pytest.mark.asyncio
    async def test_spontaneous_chime_in(self, monkeypatch):
        adapter, _ = await make_adapter(monkeypatch, require_mention=True, observe_mode=True,
                                        spontaneous_probability=1.0, spontaneous_cooldown=0.0)
        await adapter._on_irc_message(cmsg(text="what a day"))
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert "ambient channel message" in event.text
        assert "<silent>" in event.text

    @pytest.mark.asyncio
    async def test_spontaneous_cooldown_blocks_second(self, monkeypatch):
        adapter, _ = await make_adapter(monkeypatch, require_mention=True, observe_mode=True,
                                        spontaneous_probability=1.0, spontaneous_cooldown=999.0)
        await adapter._on_irc_message(cmsg(text="first"))
        await adapter._on_irc_message(cmsg(text="second"))
        assert adapter.handle_message.call_count == 1

    @pytest.mark.asyncio
    async def test_observe_off_ignores_unaddressed(self, monkeypatch):
        adapter, _ = await make_adapter(monkeypatch, require_mention=True, observe_mode=False,
                                        spontaneous_probability=1.0)
        await adapter._on_irc_message(cmsg(text="random"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_channel_context_attached(self, monkeypatch):
        adapter, _ = await make_adapter(monkeypatch, require_mention=True,
                                        dangerously_allow_name_matching=True)
        await adapter._on_irc_message(cmsg(sender_nick="bob", account=None, text="hello everyone"))
        await adapter._on_irc_message(cmsg(text="hermesx: hi"))
        event = adapter.handle_message.call_args[0][0]
        assert event.channel_context and "bob: hello everyone" in event.channel_context

    @pytest.mark.asyncio
    async def test_silent_reply_suppressed(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch)
        adapter._mark_connected()
        res = await adapter.send("#chan", "<silent>")
        assert res.success and res.message_id == "silent"
        assert not [l for l in drain(client) if l.startswith("PRIVMSG")]


# ---------------------------------------------------------------------------
# Feature B: runtime channel-agency tools
# ---------------------------------------------------------------------------

class TestAgencyTools:
    @pytest.mark.asyncio
    async def test_join_disabled_by_default(self, monkeypatch):
        adapter, _ = await make_adapter(monkeypatch, allow_agent_join=False)
        adapter._mark_connected()
        res = await adapter.runtime_join("#new")
        assert "error" in res

    @pytest.mark.asyncio
    async def test_join_enabled(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, allow_agent_join=True)
        adapter._mark_connected()
        res = await adapter.runtime_join("#new")
        assert res.get("success")
        assert "JOIN #new" in drain(client)
        assert any(c.name == "#new" for c in adapter.cfg.channels)  # persisted

    @pytest.mark.asyncio
    async def test_join_allowlist_enforced(self, monkeypatch):
        adapter, _ = await make_adapter(monkeypatch, allow_agent_join=True,
                                        joinable_channels=["#ok"])
        adapter._mark_connected()
        assert "error" in await adapter.runtime_join("#notok")
        assert (await adapter.runtime_join("#ok")).get("success")

    @pytest.mark.asyncio
    async def test_join_rejects_bad_name(self, monkeypatch):
        adapter, _ = await make_adapter(monkeypatch, allow_agent_join=True)
        adapter._mark_connected()
        assert "error" in await adapter.runtime_join("notachannel")

    @pytest.mark.asyncio
    async def test_part(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, allow_agent_join=True,
                                             channels=[ChannelSpec("#chan"), ChannelSpec("#bye")])
        adapter._mark_connected()
        res = await adapter.runtime_part("#bye")
        assert res.get("success")
        assert any(l.startswith("PART #bye") for l in drain(client))
        assert not any(c.name == "#bye" for c in adapter.cfg.channels)

    @pytest.mark.asyncio
    async def test_say_requires_membership(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch)
        adapter._mark_connected()
        # not joined -> error
        assert "error" in await adapter.runtime_say("#other", "hi")
        # join the channel in state, then say works
        await feed(client, ":hermesx!u@h JOIN #other")
        res = await adapter.runtime_say("#other", "hello")
        assert res.get("success")
        assert any("PRIVMSG #other" in l for l in drain(client))

    @pytest.mark.asyncio
    async def test_list(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch)
        await feed(client, ":hermesx!u@h JOIN #chan")
        info = adapter.runtime_list()
        assert "#chan" in info["channels"]
        assert info["nick"] == "hermesx"

    def test_tools_registered(self):
        captured = []

        class Ctx:
            def register_platform(self, **kw):
                pass

            def register_tool(self, **kw):
                captured.append(kw["name"])

        ircx.register(Ctx())
        for name in ("irc_join", "irc_part", "irc_say", "irc_list_channels"):
            assert name in captured


# ---------------------------------------------------------------------------
# Feature C: chathistory + logging persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    @pytest.mark.asyncio
    async def test_chathistory_requested_on_join(self):
        client, _ = make_client(chathistory_limit=25)
        client._cap_acked.add("draft/chathistory")
        await feed(client, ":srv 366 hermesx #chan :End of NAMES")
        out = drain(client)
        assert any(l.startswith("CHATHISTORY LATEST #chan") and "25" in l for l in out)

    @pytest.mark.asyncio
    async def test_no_chathistory_without_cap(self):
        client, _ = make_client()
        await feed(client, ":srv 366 hermesx #chan :End of NAMES")
        assert not any("CHATHISTORY" in l for l in drain(client))

    @pytest.mark.asyncio
    async def test_history_batch_flagged_and_not_dispatched(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, require_mention=False,
                                             dangerously_allow_name_matching=True)
        # Open a chathistory batch, replay a message inside it, close it.
        await feed(client,
                   ":srv BATCH +hist chathistory #chan",
                   "@batch=hist :bob!b@h PRIVMSG #chan :old message",
                   ":srv BATCH -hist")
        # The replayed message must be buffered but never answered.
        adapter.handle_message.assert_not_called()
        assert any("old message" in l for l in adapter._buf("#chan"))

    def test_logging_writes_and_replays(self, monkeypatch, tmp_path):
        _clear_irc_env(monkeypatch)
        from gateway.config import PlatformConfig
        a = IRCXAdapter(PlatformConfig(extra={"server": "s", "channels": ["#chan"]}))
        a.cfg = IRCXConfig(server="irc.test", nickname="hermesx",
                           channels=[ChannelSpec("#chan")], log_dir=str(tmp_path))
        # Need a client for casefold.
        c, _ = make_client()
        a._client = c
        a._record_line("#chan", "bob", "persisted line")
        # A fresh adapter with the same log_dir seeds its buffer from disk.
        a2 = IRCXAdapter(PlatformConfig(extra={"server": "s", "channels": ["#chan"]}))
        a2.cfg = IRCXConfig(server="irc.test", nickname="hermesx",
                            channels=[ChannelSpec("#chan")], log_dir=str(tmp_path))
        a2._client = c
        buf = a2._buf("#chan")
        assert any("bob: persisted line" in line for line in buf)


# ---------------------------------------------------------------------------
# Config parsing for new fields
# ---------------------------------------------------------------------------

class TestNewConfig:
    def test_env_parsing(self, monkeypatch):
        _clear_irc_env(monkeypatch)
        monkeypatch.setenv("IRCX_SERVER", "s")
        monkeypatch.setenv("IRCX_CHANNEL", "#c")
        monkeypatch.setenv("IRCX_OBSERVE_MODE", "true")
        monkeypatch.setenv("IRCX_SPONTANEOUS_PROBABILITY", "0.25")
        monkeypatch.setenv("IRCX_ALLOW_AGENT_JOIN", "true")
        monkeypatch.setenv("IRCX_JOINABLE_CHANNELS", "#a,#b")
        monkeypatch.setenv("IRCX_LOG_DIR", "/tmp/ircxlogs")
        from gateway.config import PlatformConfig
        cfg = ircx.load_config(PlatformConfig())
        assert cfg.observe_mode is True
        assert cfg.spontaneous_probability == 0.25
        assert cfg.allow_agent_join is True
        assert cfg.joinable_channels == ["#a", "#b"]
        assert cfg.log_dir == "/tmp/ircxlogs"

    def test_probability_clamped(self, monkeypatch):
        _clear_irc_env(monkeypatch)
        monkeypatch.setenv("IRCX_SERVER", "s")
        monkeypatch.setenv("IRCX_CHANNEL", "#c")
        monkeypatch.setenv("IRCX_SPONTANEOUS_PROBABILITY", "5")
        from gateway.config import PlatformConfig
        cfg = ircx.load_config(PlatformConfig())
        assert cfg.spontaneous_probability == 1.0


# ---------------------------------------------------------------------------
# Channel introspection: irc_channel_info / irc_whois
# ---------------------------------------------------------------------------

def _seed_roster(client):
    """Feed a NAMES + topic burst so ircstates has channel membership/ops."""
    for raw in [
        ":srv 001 pascal :hi",
        ":pascal!u@h JOIN #world-chat",
        ":srv 353 pascal = #world-chat :pascal @alice +bob carol @dave",
        ":srv 366 pascal #world-chat :end",
        ":srv 332 pascal #world-chat :Welcome to World Chat!",
        ":alice!ali@host1 JOIN #world-chat",
    ]:
        for line in client.server.recv((raw + "\r\n").encode()):
            client.server.parse_tokens(line)


class TestChannelInfo:
    @pytest.mark.asyncio
    async def test_channel_info_counts_ops_voice_topic(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, channels=[ChannelSpec("#world-chat")])
        _seed_roster(client)
        adapter._mark_connected()
        info = await adapter.runtime_channel_info("#world-chat")
        assert info["success"] is True
        assert info["channel"] == "#world-chat"
        assert info["user_count"] == 5
        assert "dave" in info["ops"]
        assert info["topic"] == "Welcome to World Chat!"
        # ops are flagged with @, voiced with + in the member list
        assert any(m.startswith("@") for m in info["members"])

    @pytest.mark.asyncio
    async def test_channel_info_defaults_to_primary_channel(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, channels=[ChannelSpec("#world-chat")])
        _seed_roster(client)
        adapter._mark_connected()
        info = await adapter.runtime_channel_info("")
        assert info.get("channel") == "#world-chat"

    @pytest.mark.asyncio
    async def test_channel_info_not_a_member(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, channels=[ChannelSpec("#world-chat")])
        adapter._mark_connected()
        res = await adapter.runtime_channel_info("#elsewhere")
        assert "error" in res

    @pytest.mark.asyncio
    async def test_whois_known_user(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, channels=[ChannelSpec("#world-chat")])
        _seed_roster(client)
        adapter._mark_connected()
        res = await adapter.runtime_whois("alice")
        assert res["success"] is True
        assert res["nick"] == "alice"
        assert "#world-chat" in res["shared_channels"]

    @pytest.mark.asyncio
    async def test_whois_unknown_user(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, channels=[ChannelSpec("#world-chat")])
        _seed_roster(client)
        adapter._mark_connected()
        res = await adapter.runtime_whois("ghost")
        assert "error" in res

    def test_new_tools_registered(self):
        captured = []

        class Ctx:
            def register_platform(self, **kw):
                pass

            def register_tool(self, **kw):
                captured.append(kw["name"])

        ircx.register(Ctx())
        assert "irc_channel_info" in captured
        assert "irc_whois" in captured

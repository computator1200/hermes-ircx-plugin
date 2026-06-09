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
            # Mirror _recv_loop: snapshot shared channels before parse_tokens
            # removes/renames the user (for QUIT/NICK event context).
            pre_channels = None
            try:
                if line.command.upper() in ("QUIT", "NICK"):
                    src = line.hostmask.nickname if line.hostmask else (
                        line.source.split("!", 1)[0] if line.source else "")
                    pre_channels = client._user_shared_channels(src)
            except Exception:
                pre_channels = None
            client.server.parse_tokens(line)
            await client._handle_line(line, pre_channels=pre_channels)


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


# ---------------------------------------------------------------------------
# Audit hardening regressions
# ---------------------------------------------------------------------------

class TestAuditFixes:
    def test_send_queue_is_bounded(self):
        client, _ = make_client()
        assert client._send_q.maxsize == 512

    @pytest.mark.asyncio
    async def test_send_line_drops_when_queue_full(self):
        client, _ = make_client()
        # Fill the queue to capacity, then one more must NOT raise.
        for _ in range(client._send_q.maxsize):
            client._send_q.put_nowait(b"x\r\n")
        client.send_line("PRIVMSG", ["#c", "overflow"])  # should warn + drop, not raise
        assert client._send_q.full()

    def test_strip_code_fence_multiline(self):
        md = "before\n```python\ncode line 1\ncode line 2\n```\nafter"
        out = ircx.strip_markdown(md)
        assert "```" not in out
        assert "code line 1" in out and "after" in out

    def test_bold_does_not_swallow_newlines(self):
        # An unterminated ** must not eat the next line (no DOTALL).
        md = "**oops\nsecond line"
        out = ircx.markdown_to_irc(md)
        assert "second line" in out
        assert "\n" in out  # newline preserved

    def test_sasl_plain_rejects_overlong_field(self):
        with pytest.raises(ircx.SASLError):
            ircx.sasl_plain_payload("user", "p" * 300)

    @pytest.mark.asyncio
    async def test_part_purges_cooldown_and_buffer(self, monkeypatch):
        adapter, client = await make_adapter(
            monkeypatch, allow_agent_join=True,
            channels=[ChannelSpec("#chan"), ChannelSpec("#gone")])
        adapter._mark_connected()
        # seed per-channel state
        adapter._last_spontaneous["#gone"] = 123.0
        adapter._buf("#gone")
        assert client.casefold("#gone") in adapter._buffers
        await adapter.runtime_part("#gone")
        assert "#gone" not in adapter._last_spontaneous
        assert client.casefold("#gone") not in adapter._buffers

    @pytest.mark.asyncio
    async def test_mid_session_433_does_not_mangle_nick(self):
        client, _ = make_client()
        # Simulate completed registration on our configured nick.
        for raw in [":srv 001 hermesx :hi"]:
            for line in client.server.recv((raw + "\r\n").encode()):
                client.server.parse_tokens(line)
                await client._handle_line(line)
        client._registered_evt.set()
        before = client._desired_nick
        await client._handle_nick_in_use()  # mid-session 433
        # Must NOT append a suffix mid-session.
        assert client._desired_nick == before

    @pytest.mark.asyncio
    async def test_nick_regain_sends_nick(self):
        client, _ = make_client(nickname="pascal")
        # Pretend we're registered but stuck on a suffixed nick.
        for raw in [":srv 001 pascal_ :hi"]:
            for line in client.server.recv((raw + "\r\n").encode()):
                client.server.parse_tokens(line)
                await client._handle_line(line)
        client._registered_evt.set()
        client._maybe_regain_nick()
        out = drain(client)
        assert any(l == "NICK pascal" for l in out)

    @pytest.mark.asyncio
    async def test_log_write_offloaded_no_loop_block(self, monkeypatch, tmp_path):
        # _record_line on the loop must schedule the write via executor and
        # the file must eventually contain the line.
        _clear_irc_env(monkeypatch)
        from gateway.config import PlatformConfig
        import asyncio as _a
        a = IRCXAdapter(PlatformConfig(extra={"server": "s", "channels": ["#chan"]}))
        a.cfg = IRCXConfig(server="irc.test", nickname="hermesx",
                           channels=[ChannelSpec("#chan")], log_dir=str(tmp_path))
        c, _ = make_client()
        a._client = c
        a._record_line("#chan", "bob", "hello disk")
        # let the executor run
        await _a.sleep(0.2)
        logs = list(tmp_path.glob("*.log"))
        assert logs and "hello disk" in logs[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Wishlist tools: topic / notice / nick / mode / kick / accounts / search
# ---------------------------------------------------------------------------

def _seed_op_roster(client, me_op=True):
    me = "@pascal" if me_op else "pascal"
    for raw in [
        ":srv 001 pascal :hi",
        ":pascal!u@h JOIN #ops",
        f":srv 353 pascal = #ops :{me} @alice bob",
        ":srv 366 pascal #ops :end",
        ":srv 324 pascal #ops +nt",
        ":srv 332 pascal #ops :the topic",
        ":bob!b@h JOIN #ops bobacct :Bob Real",  # extended-join: account in 3rd param
    ]:
        for line in client.server.recv((raw + "\r\n").encode()):
            client.server.parse_tokens(line)


class TestWishlistTools:
    @pytest.mark.asyncio
    async def test_topic_read_and_set(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, channels=[ChannelSpec("#ops")])
        _seed_op_roster(client)
        adapter._mark_connected()
        read = await adapter.runtime_topic("#ops")
        assert read["topic"] == "the topic"
        setres = await adapter.runtime_topic("#ops", "new topic")
        assert setres.get("topic_set") == "new topic"
        assert any(l.startswith("TOPIC #ops :new topic") for l in drain(client))

    @pytest.mark.asyncio
    async def test_notice_sends_notice(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, channels=[ChannelSpec("#ops")])
        _seed_op_roster(client)
        adapter._mark_connected()
        res = await adapter.runtime_notice("#ops", "automated ping")
        assert res.get("success")
        assert any(l.startswith("NOTICE #ops :automated ping") for l in drain(client))

    @pytest.mark.asyncio
    async def test_nick_change(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch)
        adapter._mark_connected()
        assert "error" in await adapter.runtime_nick("bad nick")  # space invalid
        res = await adapter.runtime_nick("pascal2")
        assert res.get("requested_nick") == "pascal2"
        assert "NICK pascal2" in drain(client)

    @pytest.mark.asyncio
    async def test_mode_read(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, channels=[ChannelSpec("#ops")])
        _seed_op_roster(client)
        adapter._mark_connected()
        res = await adapter.runtime_mode("#ops")
        assert res.get("success") and set(res["modes"]) == {"n", "t"}

    @pytest.mark.asyncio
    async def test_mode_set_requires_op(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, channels=[ChannelSpec("#ops")])
        _seed_op_roster(client, me_op=False)  # not an op
        adapter._mark_connected()
        res = await adapter.runtime_mode("#ops", "+m")
        assert "error" in res and "operator" in res["error"]

    @pytest.mark.asyncio
    async def test_mode_set_as_op(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, channels=[ChannelSpec("#ops")])
        _seed_op_roster(client, me_op=True)
        adapter._mark_connected()
        res = await adapter.runtime_mode("#ops", "+m")
        assert res.get("success")
        assert any(l.startswith("MODE #ops +m") for l in drain(client))

    @pytest.mark.asyncio
    async def test_kick_disabled_by_default(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, channels=[ChannelSpec("#ops")])
        _seed_op_roster(client, me_op=True)
        adapter._mark_connected()
        res = await adapter.runtime_kick("#ops", "bob")
        assert "error" in res and "disabled" in res["error"]

    @pytest.mark.asyncio
    async def test_kick_needs_op_even_when_allowed(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, allow_agent_kick=True,
                                             channels=[ChannelSpec("#ops")])
        _seed_op_roster(client, me_op=False)
        adapter._mark_connected()
        res = await adapter.runtime_kick("#ops", "bob")
        assert "error" in res and "operator" in res["error"]

    @pytest.mark.asyncio
    async def test_kick_works_when_allowed_and_op(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, allow_agent_kick=True,
                                             channels=[ChannelSpec("#ops")])
        _seed_op_roster(client, me_op=True)
        adapter._mark_connected()
        assert "error" in await adapter.runtime_kick("#ops", "pascal")  # refuse self
        res = await adapter.runtime_kick("#ops", "bob", "spam")
        assert res.get("kicked") == "bob"
        assert any(l.startswith("KICK #ops bob") for l in drain(client))

    @pytest.mark.asyncio
    async def test_accounts_online(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, channels=[ChannelSpec("#ops")],
                                             allow_from=["bobacct"])
        _seed_op_roster(client)
        adapter._mark_connected()
        res = await adapter.runtime_accounts_online()
        assert res.get("success")
        assert any(u["account"] == "bobacct" for u in res["online"])

    @pytest.mark.asyncio
    async def test_search_history_needs_logging(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, channels=[ChannelSpec("#ops")])
        adapter._mark_connected()
        res = await adapter.runtime_search_history("hello")
        assert "error" in res and "IRCX_LOG_DIR" in res["error"]

    @pytest.mark.asyncio
    async def test_search_history_matches(self, monkeypatch, tmp_path):
        adapter, client = await make_adapter(
            monkeypatch, channels=[ChannelSpec("#ops")], log_dir=str(tmp_path))
        adapter._mark_connected()
        adapter._append_log("#ops", "alice", "the answer is 42")
        adapter._append_log("#ops", "bob", "unrelated noise")
        adapter._append_log("#ops", "alice", "more about 42 here")
        res = await adapter.runtime_search_history("42", "#ops", sender="alice")
        assert res.get("success") and res["match_count"] == 2
        assert all(h["sender"] == "alice" for h in res["matches"])

    def test_new_tools_registered(self):
        captured = []
        class Ctx:
            def register_platform(self, **kw): pass
            def register_tool(self, **kw): captured.append(kw["name"])
        ircx.register(Ctx())
        for t in ("irc_topic", "irc_notice", "irc_nick", "irc_mode",
                  "irc_kick", "irc_accounts_online", "irc_search_history"):
            assert t in captured

    def test_kick_config_flag(self, monkeypatch):
        _clear_irc_env(monkeypatch)
        monkeypatch.setenv("IRCX_SERVER", "s")
        monkeypatch.setenv("IRCX_CHANNEL", "#c")
        monkeypatch.setenv("IRCX_ALLOW_AGENT_KICK", "true")
        from gateway.config import PlatformConfig
        cfg = ircx.load_config(PlatformConfig())
        assert cfg.allow_agent_kick is True


# ---------------------------------------------------------------------------
# Agency wishlist 2: away / cycle / set_key / whois_server / ignore
# ---------------------------------------------------------------------------

class TestAgencyWishlist2:
    @pytest.mark.asyncio
    async def test_away_set_and_clear(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch)
        adapter._mark_connected()
        res = await adapter.runtime_away("stepping back")
        assert res.get("away") is True and res["message"] == "stepping back"
        assert client.away_message == "stepping back"
        assert any(l.startswith("AWAY") and "stepping back" in l for l in drain(client))
        cleared = await adapter.runtime_away()
        assert cleared.get("away") is False
        assert client.away_message is None
        assert "AWAY" in drain(client)  # bare AWAY clears

    @pytest.mark.asyncio
    async def test_cycle_parts_then_rejoins(self, monkeypatch):
        # speed up the part/join gap
        async def _fast_sleep(*a, **k):
            return None
        monkeypatch.setattr(ircx.asyncio, "sleep", _fast_sleep)
        adapter, client = await make_adapter(
            monkeypatch, channels=[ChannelSpec("#ops", key="sekret")])
        _seed_op_roster(client)
        adapter._mark_connected()
        res = await adapter.runtime_cycle("#ops")
        assert res.get("cycled") == "#ops"
        out = drain(client)
        assert any(l.startswith("PART #ops") for l in out)
        # rejoin must carry the preserved key
        assert any(l.startswith("JOIN #ops sekret") for l in out)

    @pytest.mark.asyncio
    async def test_cycle_requires_membership(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch)
        adapter._mark_connected()
        res = await adapter.runtime_cycle("#notin")
        assert "error" in res and "not in channel" in res["error"]

    @pytest.mark.asyncio
    async def test_set_key_requires_op(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, channels=[ChannelSpec("#ops")])
        _seed_op_roster(client, me_op=False)
        adapter._mark_connected()
        res = await adapter.runtime_set_key("#ops", "hunter2")
        assert "error" in res and "operator" in res["error"]

    @pytest.mark.asyncio
    async def test_set_key_sets_clears_and_persists(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, channels=[ChannelSpec("#ops")])
        _seed_op_roster(client, me_op=True)
        adapter._mark_connected()
        # reject a key with whitespace
        assert "error" in await adapter.runtime_set_key("#ops", "bad key")
        res = await adapter.runtime_set_key("#ops", "hunter2")
        assert res.get("key_set") is True
        assert any(l.startswith("MODE #ops +k hunter2") for l in drain(client))
        # persisted onto the channel spec for reconnect rejoin
        assert adapter._channel_spec("#ops").key == "hunter2"
        cleared = await adapter.runtime_set_key("#ops")
        assert cleared.get("key_cleared") is True
        assert any(l.startswith("MODE #ops -k hunter2") for l in drain(client))
        assert adapter._channel_spec("#ops").key is None

    @pytest.mark.asyncio
    async def test_whois_server_online(self, monkeypatch):
        import asyncio
        adapter, client = await make_adapter(monkeypatch)
        adapter._mark_connected()
        task = asyncio.create_task(adapter.runtime_whois_server("stranger"))
        await asyncio.sleep(0)  # let the WHOIS get sent + slot registered
        assert any(l.startswith("WHOIS stranger") for l in drain(client))
        await feed(
            client,
            ":srv 311 pascal stranger user host * :Real Name",
            ":srv 312 pascal stranger irc.srv :A Server",
            ":srv 330 pascal stranger strangeracct :is logged in as",
            ":srv 319 pascal stranger :#a #b",
            ":srv 318 pascal stranger :End of WHOIS",
        )
        res = await task
        assert res.get("success")
        assert res["nick"] == "stranger" and res["account"] == "strangeracct"
        assert res["realname"] == "Real Name" and "#a" in res["channels"]

    @pytest.mark.asyncio
    async def test_whois_server_offline(self, monkeypatch):
        import asyncio
        adapter, client = await make_adapter(monkeypatch)
        adapter._mark_connected()
        task = asyncio.create_task(adapter.runtime_whois_server("ghost"))
        await asyncio.sleep(0)
        await feed(client, ":srv 401 pascal ghost :No such nick/channel")
        res = await task
        assert "error" in res and "not online" in res["error"]

    @pytest.mark.asyncio
    async def test_whois_server_rejects_bad_nick(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch)
        adapter._mark_connected()
        assert "error" in await adapter.runtime_whois_server("bad nick")

    @pytest.mark.asyncio
    async def test_ignore_drops_messages(self, monkeypatch):
        adapter, _ = await make_adapter(monkeypatch, require_mention=False)
        adapter._mark_connected()
        res = await adapter.runtime_ignore("troll", 120)
        assert res.get("ignored") == "troll" and res["seconds"] == 120
        await adapter._on_irc_message(cmsg(sender_nick="troll", text="noise"))
        adapter.handle_message.assert_not_called()
        # not even recorded for context
        assert not any("noise" in l for l in adapter._buf("#chan"))
        # a different sender is unaffected
        await adapter._on_irc_message(cmsg(sender_nick="alice", text="hello"))
        adapter.handle_message.assert_called()

    @pytest.mark.asyncio
    async def test_unignore_restores(self, monkeypatch):
        adapter, _ = await make_adapter(monkeypatch, require_mention=False)
        adapter._mark_connected()
        await adapter.runtime_ignore("troll", 120)
        un = await adapter.runtime_unignore("troll")
        assert un.get("was_ignored") is True
        await adapter._on_irc_message(cmsg(sender_nick="troll", text="back now"))
        adapter.handle_message.assert_called()

    @pytest.mark.asyncio
    async def test_ignore_expires(self, monkeypatch):
        import time
        adapter, _ = await make_adapter(monkeypatch, require_mention=False)
        adapter._mark_connected()
        # expiry already in the past
        adapter._ignored["troll"] = time.monotonic() - 1
        assert adapter._is_ignored("troll") is False
        assert "troll" not in adapter._ignored  # lazily purged

    def test_wishlist2_tools_registered(self):
        captured = []
        class Ctx:
            def register_platform(self, **kw): pass
            def register_tool(self, **kw): captured.append(kw["name"])
        ircx.register(Ctx())
        for t in ("irc_away", "irc_cycle", "irc_set_key", "irc_whois_server",
                  "irc_ignore", "irc_unignore"):
            assert t in captured


# ---------------------------------------------------------------------------
# Membership events: join / part / quit / kick / nick visibility in context
# ---------------------------------------------------------------------------

async def _join_chan(client, chan, *nicks):
    """Put the bot + given nicks into a channel via NAMES."""
    roster = "hermesx " + " ".join(nicks)
    await feed(
        client,
        f":hermesx!u@h JOIN {chan}",
        f":srv 353 hermesx = {chan} :{roster}",
        f":srv 366 hermesx {chan} :end",
    )


class TestMembershipEvents:
    @pytest.mark.asyncio
    async def test_join_recorded_to_context(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, show_events=True)
        await _join_chan(client, "#chan")
        await feed(client, ":alice!a@h JOIN #chan")
        assert any("alice has joined #chan" in l for l in adapter._buf("#chan"))
        adapter.handle_message.assert_not_called()  # never dispatched as a prompt

    @pytest.mark.asyncio
    async def test_part_recorded_with_reason(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, show_events=True)
        await _join_chan(client, "#chan", "alice")
        await feed(client, ":alice!a@h PART #chan :brb")
        assert any("alice has left #chan (brb)" in l for l in adapter._buf("#chan"))

    @pytest.mark.asyncio
    async def test_quit_recorded_to_all_shared_channels(self, monkeypatch):
        adapter, client = await make_adapter(
            monkeypatch, show_events=True,
            channels=[ChannelSpec("#chan"), ChannelSpec("#ops")])
        await _join_chan(client, "#chan", "alice")
        await _join_chan(client, "#ops", "alice")
        await feed(client, ":alice!a@h QUIT :leaving")
        assert any("alice has quit IRC (leaving)" in l for l in adapter._buf("#chan"))
        assert any("alice has quit IRC (leaving)" in l for l in adapter._buf("#ops"))

    @pytest.mark.asyncio
    async def test_nick_change_recorded(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, show_events=True)
        await _join_chan(client, "#chan", "alice")
        await feed(client, ":alice!a@h NICK alice2")
        assert any("alice is now known as alice2" in l for l in adapter._buf("#chan"))

    @pytest.mark.asyncio
    async def test_kick_recorded(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, show_events=True)
        await _join_chan(client, "#chan", "bob")
        await feed(client, ":alice!a@h KICK #chan bob :spam")
        assert any("bob was kicked from #chan by alice (spam)" in l
                   for l in adapter._buf("#chan"))

    @pytest.mark.asyncio
    async def test_own_join_not_recorded(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, show_events=True)
        await _join_chan(client, "#chan")  # includes the bot's own JOIN
        assert not any("hermesx has joined" in l for l in adapter._buf("#chan"))

    @pytest.mark.asyncio
    async def test_disabled_by_default(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch)  # show_events defaults off
        await _join_chan(client, "#chan")
        await feed(client, ":alice!a@h JOIN #chan")
        assert not any("alice has joined" in l for l in adapter._buf("#chan"))

    @pytest.mark.asyncio
    async def test_event_appears_in_formatted_context(self, monkeypatch):
        adapter, client = await make_adapter(monkeypatch, show_events=True,
                                             require_mention=False)
        await _join_chan(client, "#chan", "alice")
        await feed(client, ":alice!a@h JOIN #chan")  # recorded as event
        # a later real message triggers a turn; the join should be in context
        await adapter._on_irc_message(cmsg(sender_nick="alice", text="hello"))
        ctx = adapter._format_context("#chan")
        assert ctx and "alice has joined #chan" in ctx

    def test_show_events_config_flag(self, monkeypatch):
        _clear_irc_env(monkeypatch)
        monkeypatch.setenv("IRCX_SERVER", "s")
        monkeypatch.setenv("IRCX_CHANNEL", "#c")
        monkeypatch.setenv("IRCX_SHOW_EVENTS", "true")
        from gateway.config import PlatformConfig
        cfg = ircx.load_config(PlatformConfig())
        assert cfg.show_events is True

"""Unit tests for src/slack_daemon.py — Feature C reaction wiring."""

import asyncio

from security import AccessControl, SecurityConfig
from slack_daemon import SlackDaemon


class FakeSlackClient:
    """Records reactions_add/remove and chat_postMessage calls."""

    def __init__(self) -> None:
        self.added: list[dict] = []
        self.removed: list[dict] = []
        self.posted: list[dict] = []

    async def reactions_add(self, **kwargs):
        self.added.append(kwargs)
        return {"ok": True}

    async def reactions_remove(self, **kwargs):
        self.removed.append(kwargs)
        return {"ok": True}

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ok": True, "ts": "posted.0"}


def make_daemon(monkeypatch) -> SlackDaemon:
    """Build a SlackDaemon without opening real Slack/socket connections."""
    import slack_daemon as sd

    # Neuter the slack-bolt app/handler constructors so __init__ does no I/O.
    class _FakeApp:
        def __init__(self, *a, **k):
            self.client = FakeSlackClient()

        def event(self, _name):  # decorator factory; registration is a no-op here
            def _wrap(fn):
                return fn
            return _wrap

        def action(self, _action_id):  # block_actions registration is a no-op
            def _wrap(fn):
                return fn
            return _wrap

    monkeypatch.setattr(sd, "AsyncApp", _FakeApp)
    monkeypatch.setattr(sd, "AsyncSocketModeHandler", lambda *a, **k: object())
    return SlackDaemon(bot_token="xoxb-test", app_token="xapp-test")


class TestTriggerMap:
    def test_trigger_to_thread_map_exists(self, monkeypatch):
        d = make_daemon(monkeypatch)
        assert d._trigger_to_thread == {}


class TestReactionLifecycle:
    def test_new_message_adds_then_removes_octagonal_sign(self, monkeypatch):
        d = make_daemon(monkeypatch)

        async def fake_handle_message(channel, message_ts, text, files=None, progress_cb=None):
            return "the reply"

        async def fake_post(channel, thread_ts, text):
            return None

        monkeypatch.setattr(d._claude, "handle_message", fake_handle_message)
        monkeypatch.setattr(d, "_post_response", fake_post)

        asyncio.run(d._handle_claude_new_message("C1", "100.1", "hi", "100.1"))

        client = d._app.client
        assert client.added == [
            {"channel": "C1", "name": "octagonal_sign", "timestamp": "100.1"}
        ]
        assert client.removed == [
            {"channel": "C1", "name": "octagonal_sign", "timestamp": "100.1"}
        ]

    def test_thread_reply_uses_reply_ts_as_trigger(self, monkeypatch):
        d = make_daemon(monkeypatch)

        async def fake_reply(channel, thread_ts, text, files=None, progress_cb=None):
            return "reply"

        async def fake_post(channel, thread_ts, text):
            return None

        monkeypatch.setattr(d._claude, "handle_thread_reply", fake_reply)
        monkeypatch.setattr(d, "_post_response", fake_post)

        # thread_ts (root) = "100.1", but the reply's own ts = "200.9"
        asyncio.run(d._handle_claude_thread_reply("C1", "100.1", "more", "200.9"))

        client = d._app.client
        assert client.added[0]["timestamp"] == "200.9"
        assert client.removed[0]["timestamp"] == "200.9"
        # reverse map links trigger_ts → thread_ts during the run
        # (cleared in finally; assert it was populated via the add side effect)

    def test_reaction_removed_even_on_error(self, monkeypatch):
        d = make_daemon(monkeypatch)

        async def boom(channel, message_ts, text, files=None, progress_cb=None):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(d._claude, "handle_message", boom)

        asyncio.run(d._handle_claude_new_message("C1", "100.1", "hi", "100.1"))

        assert d._app.client.removed[0]["timestamp"] == "100.1"


class TestReactionAddedHandler:
    def _event(self, reaction="octagonal_sign", user="U_human", ts="trig.1", channel="C1"):
        return {
            "reaction": reaction,
            "user": user,
            "item": {"type": "message", "channel": channel, "ts": ts},
        }

    def test_stops_run_and_posts_stopped_notice(self, monkeypatch):
        d = make_daemon(monkeypatch)
        d._bot_user_id = "U_bot"
        d._trigger_to_thread["trig.1"] = "thread.1"

        stopped = []

        async def fake_stop(thread_ts):
            stopped.append(thread_ts)
            return True

        monkeypatch.setattr(d._claude, "stop", fake_stop)

        asyncio.run(d._handle_reaction_added(self._event()))

        assert stopped == ["thread.1"]
        assert d._app.client.posted == [
            {"channel": "C1", "thread_ts": "thread.1", "text": "⏹️ Stopped."}
        ]

    def test_ignores_bot_own_reaction(self, monkeypatch):
        d = make_daemon(monkeypatch)
        d._bot_user_id = "U_bot"
        d._trigger_to_thread["trig.1"] = "thread.1"

        called = []
        monkeypatch.setattr(d._claude, "stop", lambda t: called.append(t))

        asyncio.run(d._handle_reaction_added(self._event(user="U_bot")))

        assert called == []
        assert d._app.client.posted == []

    def test_ignores_other_emoji(self, monkeypatch):
        d = make_daemon(monkeypatch)
        d._bot_user_id = "U_bot"
        d._trigger_to_thread["trig.1"] = "thread.1"

        called = []
        monkeypatch.setattr(d._claude, "stop", lambda t: called.append(t))

        asyncio.run(d._handle_reaction_added(self._event(reaction="thumbsup")))

        assert called == []
        assert d._app.client.posted == []

    def test_unknown_trigger_ts_is_noop(self, monkeypatch):
        d = make_daemon(monkeypatch)
        d._bot_user_id = "U_bot"
        # _trigger_to_thread empty → unknown ts

        called = []
        monkeypatch.setattr(d._claude, "stop", lambda t: called.append(t))

        asyncio.run(d._handle_reaction_added(self._event(ts="ghost.0")))

        assert called == []
        assert d._app.client.posted == []


class TestStoppedSuppression:
    def test_stopped_thread_skips_normal_reply(self, monkeypatch):
        d = make_daemon(monkeypatch)

        async def fake_handle_message(channel, message_ts, text, files=None, progress_cb=None):
            # Simulate the user having stopped this run mid-flight.
            d._claude._stopped.add(message_ts)
            return "partial reply that must NOT be posted"

        posted = []

        async def fake_post(channel, thread_ts, text):
            posted.append((thread_ts, text))

        monkeypatch.setattr(d._claude, "handle_message", fake_handle_message)
        monkeypatch.setattr(d, "_post_response", fake_post)

        asyncio.run(d._handle_claude_new_message("C1", "100.1", "hi", "100.1"))

        assert posted == []  # reply suppressed
        assert "100.1" not in d._claude._stopped  # flag cleared for next run

    def test_normal_run_still_posts(self, monkeypatch):
        d = make_daemon(monkeypatch)

        async def fake_handle_message(channel, message_ts, text, files=None, progress_cb=None):
            return "normal reply"

        posted = []

        async def fake_post(channel, thread_ts, text):
            posted.append((thread_ts, text))

        monkeypatch.setattr(d._claude, "handle_message", fake_handle_message)
        monkeypatch.setattr(d, "_post_response", fake_post)

        asyncio.run(d._handle_claude_new_message("C1", "100.1", "hi", "100.1"))

        assert posted == [("100.1", "normal reply")]

    def test_stopped_flag_cleared_even_when_run_raises(self, monkeypatch):
        # If the run is marked stopped and then raises before the suppression
        # check, the flag must still be cleared (in finally) so a LATER run on
        # the same thread_ts is not silently suppressed.
        d = make_daemon(monkeypatch)

        async def boom(channel, message_ts, text, files=None, progress_cb=None):
            d._claude._stopped.add(message_ts)
            raise RuntimeError("kaboom")

        monkeypatch.setattr(d._claude, "handle_message", boom)

        asyncio.run(d._handle_claude_new_message("C1", "100.1", "hi", "100.1"))

        assert "100.1" not in d._claude._stopped  # cleared despite the exception

    def test_stopped_thread_reply_skips_normal_reply(self, monkeypatch):
        # Mirror of the new-message suppression, for the thread-reply path
        # (keyed on thread_ts, with a distinct trigger_ts).
        d = make_daemon(monkeypatch)

        async def fake_reply(channel, thread_ts, text, files=None, progress_cb=None):
            d._claude._stopped.add(thread_ts)
            return "partial reply that must NOT be posted"

        posted = []

        async def fake_post(channel, thread_ts, text):
            posted.append((thread_ts, text))

        monkeypatch.setattr(d._claude, "handle_thread_reply", fake_reply)
        monkeypatch.setattr(d, "_post_response", fake_post)

        asyncio.run(d._handle_claude_thread_reply("C1", "100.1", "more", "200.9"))

        assert posted == []  # reply suppressed
        assert "100.1" not in d._claude._stopped  # flag cleared for next run


class TestLiveProgressToggle:
    def test_reporter_built_when_on(self, monkeypatch):
        import slack_daemon as sd
        d = make_daemon(monkeypatch)
        monkeypatch.setattr(sd, "LIVE_PROGRESS", True)
        assert d._make_reporter("C1", "T1", "T1") is not None

    def test_no_reporter_when_off(self, monkeypatch):
        import slack_daemon as sd
        d = make_daemon(monkeypatch)
        monkeypatch.setattr(sd, "LIVE_PROGRESS", False)
        assert d._make_reporter("C1", "T1", "T1") is None


class TestStatusReactionStop:
    def test_status_post_wires_reaction_and_drops_trigger(self, monkeypatch):
        import slack_daemon as sd
        d = make_daemon(monkeypatch)
        monkeypatch.setattr(sd, "LIVE_PROGRESS", True)
        reporter = d._make_reporter("C1", "thread.1", "trig.1")

        # Simulate the reporter's first post invoking its on_status_posted hook.
        asyncio.run(reporter._on_status_posted("status.9"))

        # Reacting 🛑 on the status message now resolves to the run...
        assert d._trigger_to_thread["status.9"] == "thread.1"
        # ...the bot put 🛑 on the status message...
        assert {"channel": "C1", "name": "octagonal_sign", "timestamp": "status.9"} \
            in d._app.client.added
        # ...and removed the now-redundant trigger-message reaction.
        assert {"channel": "C1", "name": "octagonal_sign", "timestamp": "trig.1"} \
            in d._app.client.removed

    def test_reaction_on_status_message_stops_run(self, monkeypatch):
        d = make_daemon(monkeypatch)
        d._bot_user_id = "U_bot"
        d._trigger_to_thread["status.9"] = "thread.1"  # as wired when status posted
        stopped = []

        async def fake_stop(t):
            stopped.append(t)
            return True

        monkeypatch.setattr(d._claude, "stop", fake_stop)
        ev = {"reaction": "octagonal_sign", "user": "U_human",
              "item": {"channel": "C1", "ts": "status.9"}}

        asyncio.run(d._handle_reaction_added(ev))

        assert stopped == ["thread.1"]
        assert d._app.client.posted == [
            {"channel": "C1", "thread_ts": "thread.1", "text": "⏹️ Stopped."}
        ]

    def test_unwind_clears_mapping_and_reaction_when_message_present(self, monkeypatch):
        import slack_daemon as sd
        d = make_daemon(monkeypatch)
        reporter = sd._ProgressReporter(d._app.client, "C1", "thread.1")
        reporter.posted_ts = "status.9"
        reporter._status_ts = "status.9"  # still present (finished as summary)
        d._trigger_to_thread["status.9"] = "thread.1"

        asyncio.run(d._unwind_status_stop("C1", reporter))

        assert "status.9" not in d._trigger_to_thread
        assert {"channel": "C1", "name": "octagonal_sign", "timestamp": "status.9"} \
            in d._app.client.removed


class FakeWriter:
    """Records what Case 1 writes to a blocked session; close() is tracked."""

    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, chunk: bytes) -> None:
        self.data += chunk

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class TestPendingReplyAccess:
    """Replies answering a pending ask_on_slack session skip the channel check."""

    @staticmethod
    def _strict_ac(**kwargs) -> AccessControl:
        return AccessControl(SecurityConfig(enabled=True, strict_mode=True, **kwargs))

    @staticmethod
    def _reply(user="U-designer", channel="D-dm", thread="thr.1", text="option B"):
        return {"user": user, "channel": channel, "thread_ts": thread,
                "ts": "reply.1", "text": text}

    def test_listed_user_answers_from_unlisted_channel(self, monkeypatch):
        # The scenario the responder path exists for: the user is allowlisted,
        # the DM the question was asked in is not, strict mode is on.
        d = make_daemon(monkeypatch)
        d._access_control = self._strict_ac(allowed_users={"U-designer"})
        writer = FakeWriter()
        d._pending["thr.1"] = writer

        asyncio.run(d._handle_slack_message(self._reply()))

        assert writer.data == b"option B\n"
        assert writer.closed is True
        assert "thr.1" not in d._pending
        assert d._app.client.posted == []  # no rejection

    def test_unlisted_user_is_rejected_and_session_stays_pending(self, monkeypatch):
        # A denied answer must not consume the session: the person the question
        # was meant for can still reply after a stranger was turned away.
        d = make_daemon(monkeypatch)
        d._access_control = self._strict_ac(allowed_users={"U-designer"})
        writer = FakeWriter()
        d._pending["thr.1"] = writer

        asyncio.run(d._handle_slack_message(self._reply(user="U-stranger")))

        assert writer.data == b""
        assert d._pending["thr.1"] is writer
        assert len(d._app.client.posted) == 1
        assert d._app.client.posted[0]["thread_ts"] == "thr.1"

    def test_same_user_without_pending_session_gets_full_check(self, monkeypatch):
        # No pending session means the reply could start a Claude run, so the
        # channel allowlist applies again and the unlisted DM is rejected.
        d = make_daemon(monkeypatch)
        d._access_control = self._strict_ac(allowed_users={"U-designer"})

        asyncio.run(d._handle_slack_message(self._reply(thread="thr.unknown")))

        assert len(d._app.client.posted) == 1

    def test_app_mention_answer_is_forwarded_not_rejected(self, monkeypatch):
        # An answer that @mentions the bot also arrives as an app_mention
        # event. That handler must not re-gate it on the channel allowlist —
        # access control lives in the message handler it delegates to.
        d = make_daemon(monkeypatch)
        d._access_control = self._strict_ac(allowed_users={"U-designer"})
        writer = FakeWriter()
        d._pending["thr.1"] = writer

        asyncio.run(d._handle_app_mention(self._reply(text="<@U_bot> option B")))

        assert writer.data == b"<@U_bot> option B\n"
        assert d._app.client.posted == []

    def test_admin_in_allowed_channel_unaffected(self, monkeypatch):
        # Regression guard: the ordinary path through is_allowed still works.
        d = make_daemon(monkeypatch)
        d._access_control = self._strict_ac(
            allowed_users={"U-admin"}, admin_users={"U-admin"},
        )
        writer = FakeWriter()
        d._pending["thr.1"] = writer

        asyncio.run(d._handle_slack_message(self._reply(user="U-admin")))

        assert writer.data == b"option B\n"
        assert d._app.client.posted == []


def _daemon_warnings(caplog) -> list:
    """WARNING records from slack_daemon only (daemon construction logs elsewhere)."""
    return [
        r for r in caplog.records
        if r.name == "slack_daemon" and r.levelname == "WARNING"
    ]


class TestBenignReactionErrors:
    """Slack errors meaning "already in the desired state" must not warn."""

    @staticmethod
    def _api_error(code: str):
        from slack_sdk.errors import SlackApiError

        return SlackApiError("failed", {"ok": False, "error": code})

    def test_no_reaction_on_remove_is_debug(self, monkeypatch, caplog):
        d = make_daemon(monkeypatch)

        async def _remove(**kwargs):
            raise self._api_error("no_reaction")

        d._app.client.reactions_remove = _remove
        with caplog.at_level("DEBUG", logger="slack_daemon"):
            asyncio.run(d._remove_stop_reaction("C1", "1.0"))
        assert not _daemon_warnings(caplog)
        assert any("No stop reaction left" in r.message for r in caplog.records)

    def test_already_reacted_on_add_is_debug(self, monkeypatch, caplog):
        d = make_daemon(monkeypatch)

        async def _add(**kwargs):
            raise self._api_error("already_reacted")

        d._app.client.reactions_add = _add
        with caplog.at_level("DEBUG", logger="slack_daemon"):
            asyncio.run(d._add_stop_reaction("C1", "1.0"))
        assert not _daemon_warnings(caplog)

    def test_other_slack_error_still_warns(self, monkeypatch, caplog):
        d = make_daemon(monkeypatch)

        async def _remove(**kwargs):
            raise self._api_error("channel_not_found")

        d._app.client.reactions_remove = _remove
        with caplog.at_level("WARNING", logger="slack_daemon"):
            asyncio.run(d._remove_stop_reaction("C1", "1.0"))
        assert any(
            r.levelname == "WARNING" and "Failed to remove stop reaction" in r.message
            for r in caplog.records
        )

import os

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from yahoomail_mcp import server, yahoo


def test_search_requires_both_pagination_values(monkeypatch):
    with pytest.raises(ToolError, match="both before_uid"):
        server.search_mail(before_uid=10)


def test_search_rejects_control_characters(monkeypatch):
    with pytest.raises(ToolError, match="control characters"):
        server.search_mail(query="bad\x00")


def test_require_writes_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("YAHOO_ENABLE_WRITES", raising=False)
    with pytest.raises(yahoo.MailError, match="Writes are disabled"):
        server.require_writes()


def test_account_setup_instructions_never_returns_credentials(monkeypatch):
    monkeypatch.delenv("YAHOO_EMAIL", raising=False)
    monkeypatch.delenv("YAHOO_APP_PASSWORD", raising=False)
    result = server.account_setup_instructions()
    assert result["missing"] == ["YAHOO_EMAIL", "YAHOO_APP_PASSWORD"]
    assert "password" in result["steps"][-1].lower()
    assert "value" not in result


def test_analyze_senders_groups_by_list_id(monkeypatch):
    class Context:
        def __enter__(self): return object()
        def __exit__(self, *args): return False

    monkeypatch.setattr(server.yahoo, "connection", lambda: Context())
    monkeypatch.setattr(server.yahoo, "select", lambda *args: 7)
    monkeypatch.setattr(server.yahoo, "search_uids", lambda *args: [3, 2])
    monkeypatch.setattr(server.yahoo, "headers", lambda *args: {
        3: {"List-ID": "news.example", "From": "news@example.com"},
        2: {"List-ID": "news.example", "From": "news@example.com"},
    })
    monkeypatch.setattr(server.yahoo, "metadata", lambda *args: {
        3: {"size": 20, "unread": True}, 2: {"size": 30, "unread": True},
    })
    result = server.analyze_senders(min_messages=2, min_unread=0, min_total_bytes=0)
    assert result["uid_validity"] == 7
    assert result["groups"][0]["list_id"] == "news.example"
    assert result["groups"][0]["message_count"] == 2
    assert result["groups"][0]["total_bytes"] == 50


def test_trash_preview_and_single_use_approval(monkeypatch):
    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return False

    monkeypatch.setenv("YAHOO_ENABLE_WRITES", "1")
    monkeypatch.setattr(server.yahoo, "connection", lambda: Context())
    monkeypatch.setattr(server.yahoo, "require_validity", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.yahoo, "headers", lambda *args: {1: {"Subject": "Test"}})
    monkeypatch.setattr(server.yahoo, "summary", lambda uid, message: {"uid": uid, "subject": "Test"})
    monkeypatch.setattr(server.secrets, "token_urlsafe", lambda n: "preview")
    monkeypatch.setattr(server.secrets, "randbelow", lambda n: 1234567)
    monkeypatch.setattr(server.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(server.yahoo, "move_to_trash", lambda *args: {"move_accepted": True})
    server.pending.clear()

    preview = server.trash_messages(folder="INBOX", uid_validity=1, uids=[1])
    assert preview["preview_id"] == "preview"
    assert preview["count"] == 1
    assert server.trash_messages(preview_id="preview", approval_code="11234567") == {"move_accepted": True}
    with pytest.raises(ToolError, match="expired or already used"):
        server.trash_messages(preview_id="preview", approval_code="11234567")


def test_search_returns_page_and_cursor(monkeypatch):
    class Context:
        def __enter__(self): return object()
        def __exit__(self, *args): return False
    monkeypatch.setattr(server.yahoo, "connection", lambda: Context())
    monkeypatch.setattr(server.yahoo, "select", lambda *args: 7)
    monkeypatch.setattr(server.yahoo, "search_uids", lambda *args: [3, 2, 1])
    monkeypatch.setattr(server.yahoo, "headers", lambda *args: {3: {}, 2: {}})
    monkeypatch.setattr(server.yahoo, "summary", lambda uid, message: {"uid": uid})
    result = server.search_mail(query="x", limit=2)
    assert result["messages"] == [{"uid": 3}, {"uid": 2}]
    assert result["next_before_uid"] == 2


def test_search_rejects_stale_folder(monkeypatch):
    class Context:
        def __enter__(self): return object()
        def __exit__(self, *args): return False
    monkeypatch.setattr(server.yahoo, "connection", lambda: Context())
    monkeypatch.setattr(server.yahoo, "select", lambda *args: 9)
    with pytest.raises(ToolError, match="folder changed"):
        server.search_mail(query="x", before_uid=10, uid_validity=8)


def test_sender_analysis_and_cleanup_suggestions_cover_sender_groups(monkeypatch):
    class Context:
        def __enter__(self): return object()
        def __exit__(self, *args): return False
    monkeypatch.setattr(server.yahoo, "connection", lambda: Context())
    monkeypatch.setattr(server.yahoo, "select", lambda *args: 7)
    monkeypatch.setattr(server.yahoo, "search_uids", lambda *args: [3, 2, 1])
    monkeypatch.setattr(server.yahoo, "headers", lambda *args: {
        3: {"From": "News <news@example.com>", "List-Unsubscribe": "<https://example.com/out>"},
        2: {"From": "news@example.com"},
        1: {"From": "other@example.com", "List-ID": "other.example"},
    })
    monkeypatch.setattr(server.yahoo, "metadata", lambda *args: {
        3: {"size": 200, "unread": True}, 2: {"size": 50, "unread": False},
        1: {"size": 500, "unread": True},
    })
    monkeypatch.setattr(server.yahoo, "summary", lambda uid, message: {"uid": uid})
    groups = server.analyze_senders(min_messages=2, min_unread=0, min_total_bytes=0)
    assert groups["groups"][0]["sender"] == "news@example.com"
    assert groups["groups"][0]["unsubscribe"] == ["https://example.com/out"]
    suggestions = server.suggest_cleanup(min_size_bytes=100)
    assert [item["uid"] for item in suggestions["candidates"]] == [3, 1]


def test_unsubscribe_options_matches_sender_and_list_id(monkeypatch):
    class Context:
        def __enter__(self): return object()
        def __exit__(self, *args): return False
    monkeypatch.setattr(server.yahoo, "connection", lambda: Context())
    monkeypatch.setattr(server.yahoo, "select", lambda *args: 7)
    monkeypatch.setattr(server.yahoo, "search_uids", lambda *args: [2, 1])
    monkeypatch.setattr(server.yahoo, "headers", lambda *args: {
        2: {"From": "news@example.com", "List-ID": "news.example", "List-Unsubscribe": "<mailto:leave@example.com>"},
        1: {"From": "other@example.com"},
    })
    monkeypatch.setattr(server.yahoo, "metadata", lambda *args: {
        2: {"size": 10, "unread": True}, 1: {"size": 10, "unread": False},
    })
    result = server.unsubscribe_options("news.example")
    assert result["matched_uids"] == [2]
    assert result["unsubscribe_options"] == ["mailto:leave@example.com"]


def test_read_thread_reports_missing_seed_and_failed_related_message(monkeypatch):
    class Context:
        def __enter__(self): return object()
        def __exit__(self, *args): return False
    monkeypatch.setattr(server.yahoo, "connection", lambda: Context())
    monkeypatch.setattr(server.yahoo, "require_validity", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.yahoo, "headers", lambda *args: {})
    with pytest.raises(ToolError, match="no longer in this folder"):
        server.read_thread("INBOX", 5, 1)
    monkeypatch.setattr(server.yahoo, "headers", lambda *args: {5: {"Message-ID": "<x>"}})
    monkeypatch.setattr(server.yahoo, "related_uids", lambda *args: {5, 4})
    monkeypatch.setattr(server.yahoo, "read", lambda conn, uid: (_ for _ in ()).throw(yahoo.MailError("gone")) if uid == 4 else {"uid": uid})
    result = server.read_thread("INBOX", 5, 1)
    assert {message["uid"] for message in result["messages"]} == {4, 5}


def test_save_draft_rejects_partial_reply(monkeypatch):
    monkeypatch.setenv("YAHOO_ENABLE_WRITES", "1")
    with pytest.raises(ToolError, match="supply reply_folder"):
        server.save_draft("to@example.com", "subject", "body", reply_folder="INBOX")


def test_read_message_and_thread_use_folder_validity(monkeypatch):
    class Context:
        def __enter__(self): return object()
        def __exit__(self, *args): return False
    monkeypatch.setattr(server.yahoo, "connection", lambda: Context())
    monkeypatch.setattr(server.yahoo, "require_validity", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.yahoo, "read", lambda conn, uid: {"uid": uid})
    monkeypatch.setattr(server.yahoo, "headers", lambda *args: {5: {"Message-ID": "<x>"}})
    monkeypatch.setattr(server.yahoo, "related_uids", lambda *args: {5, 4})
    assert server.read_message("INBOX", 5, 1) == {"uid": 5}
    result = server.read_thread("INBOX", 5, 1)
    assert [message["uid"] for message in result["messages"]] == [4, 5]


def test_save_draft_success_and_invalid_recipient_are_tool_errors(monkeypatch):
    class Context:
        def __enter__(self): return object()
        def __exit__(self, *args): return False
    monkeypatch.setenv("YAHOO_ENABLE_WRITES", "1")
    monkeypatch.setattr(server.yahoo, "connection", lambda: Context())
    monkeypatch.setattr(server.yahoo, "append_draft", lambda *args: {"saved": True})
    assert server.save_draft("to@example.com", "subject", "body") == {"saved": True}
    monkeypatch.setattr(server.yahoo, "append_draft", lambda *args: (_ for _ in ()).throw(ValueError()))
    with pytest.raises(ToolError, match="Invalid draft headers"):
        server.save_draft("to@example.com", "subject", "body")


def test_remote_approval_uses_link_token_and_consumes_before_imap(monkeypatch):
    from contextlib import contextmanager

    from starlette.testclient import TestClient

    token = "browser-token"
    approval = {"folder": "INBOX", "uid_validity": 1, "uids": [7], "messages": []}
    events = []

    class Store:
        def get(self, approval_id, supplied):
            if approval_id == "preview" and supplied == token and "consume" not in events:
                return approval
            return None

        def consume(self, approval_id, supplied):
            item = self.get(approval_id, supplied)
            if item:
                events.append("consume")
            return item

    @contextmanager
    def connect():
        events.append("connect")
        yield object()

    monkeypatch.setattr(server, "remote_approval_store", None)
    monkeypatch.setattr(server, "approval_base_url", "")
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    monkeypatch.setattr(yahoo, "connection", connect)
    monkeypatch.setattr(yahoo, "move_to_trash", lambda *args: {"requested_uids": [7]})
    app = server.create_app("a" * 32, approval_store=Store())
    with TestClient(app) as client:
        assert client.post("/mcp").status_code == 401
        assert client.get("/approve/preview?token=wrong").status_code == 404
        response = client.get(f"/approve/preview?token={token}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "Approve move to Trash" in response.text
        assert client.post(f"/approve/preview?token={token}").status_code == 404
        assert events == []
        assert client.post("/approve/preview", data={"token": token}).status_code == 200
        assert client.post("/approve/preview", data={"token": token}).status_code == 404
        assert events == ["consume", "connect"]


def test_lambda_defaults_to_read_only(monkeypatch):
    from yahoomail_mcp import lambda_server

    monkeypatch.setenv("MCP_REMOTE_HOST", "example.com")
    monkeypatch.setenv("YAHOO_ENABLE_WRITES", "1")
    monkeypatch.setenv("YAHOO_EMAIL", "old@example.com")
    monkeypatch.setenv("YAHOO_APP_PASSWORD", "old")
    monkeypatch.setattr(lambda_server, "create_app", lambda token, **kwargs: kwargs)
    app = lambda_server.configure({
        "YAHOO_MCP_TOKEN": "a" * 32,
        "YAHOO_EMAIL": "test@example.com",
        "YAHOO_APP_PASSWORD": "fake",
    })
    assert app["approval_store"] is None
    with pytest.raises(yahoo.MailError, match="Writes are disabled"):
        server.require_writes()


def test_lambda_multi_user_configures_oauth_and_user_table(monkeypatch):
    from yahoomail_mcp import lambda_server

    class Table:
        pass

    monkeypatch.setenv("MCP_REMOTE_HOST", "example.com")
    monkeypatch.setenv("YAHOO_OAUTH_REDIRECT_URI", "https://example.com/oauth/callback")
    monkeypatch.setattr(lambda_server, "create_app", lambda **kwargs: kwargs)
    user_table = Table()
    app = lambda_server.configure(
        {
            "YAHOO_OAUTH_CLIENT_ID": "client",
            "YAHOO_OAUTH_CLIENT_SECRET": "secret",
        },
        user_table=user_table,
        mode="multi-user",
    )
    assert app["multi_user"].config.client_id == "client"
    assert app["multi_user"].store.table is user_table
    assert app["approval_store"] is None
    assert os.environ["YAHOO_ENABLE_WRITES"] == "0"


def test_lambda_multi_user_requires_user_table(monkeypatch):
    from yahoomail_mcp import lambda_server

    monkeypatch.setenv("MCP_REMOTE_HOST", "example.com")
    with pytest.raises(ValueError, match="requires a user table"):
        lambda_server.configure(
            {"YAHOO_OAUTH_CLIENT_ID": "client", "YAHOO_OAUTH_CLIENT_SECRET": "secret"},
            mode="multi-user",
        )


def test_lambda_configuration_rejects_missing_credentials(monkeypatch):
    from yahoomail_mcp import lambda_server

    monkeypatch.setenv("MCP_REMOTE_HOST", "example.com")
    with pytest.raises(KeyError):
        lambda_server.configure({})
    with pytest.raises(ValueError, match="YAHOO_OAUTH_CLIENT_SECRET"):
        lambda_server.configure({"YAHOO_OAUTH_CLIENT_ID": "client"}, mode="multi-user", user_table=object())


def test_multi_user_oauth_routes_and_authentication(monkeypatch):
    from starlette.testclient import TestClient

    class Sessions:
        def get_session(self, token):
            return {"user_id": "user-1"} if token == "session" else None

    class Broker:
        store = Sessions()

        def start(self, return_to):
            return "https://login.yahoo.com/oauth?state=state", "state"

        def finish(self, code, state):
            assert (code, state) == ("code", "state")
            return "session", "/"

        def mailbox(self, user_id):
            assert user_id == "user-1"
            return "user@example.com", "access-token"

    monkeypatch.setattr(server.mcp, "_session_manager", None)
    app = server.create_app(remote_host="example.com", multi_user=Broker())
    with TestClient(app, base_url="https://example.com") as client:
        response = client.get("/oauth/start", follow_redirects=False)
        assert response.status_code == 307
        assert "login.yahoo.com" in response.headers["location"]
        callback = client.get("/oauth/callback?code=code&state=state")
        assert callback.status_code == 200
        assert "session" in callback.text
        assert callback.headers["cache-control"] == "no-store"
        assert client.post("/mcp").status_code == 401
        assert client.post("/mcp", headers={"Authorization": "Bearer bad"}).status_code == 401


def test_multi_user_remote_approval_uses_the_authorized_mailbox(monkeypatch):
    from contextlib import contextmanager
    from starlette.testclient import TestClient

    class ApprovalStore:
        def consume(self, approval_id, token):
            assert (approval_id, token) == ("preview", "token")
            return {"folder": "INBOX", "uid_validity": 1, "uids": [7], "messages": [], "user_id": "user-1"}

    class Broker:
        def mailbox(self, user_id):
            assert user_id == "user-1"
            return "user@example.com", "access-token"

    @contextmanager
    def connect():
        yield object()

    monkeypatch.setattr(server.yahoo, "connection", connect)
    monkeypatch.setattr(server.yahoo, "move_to_trash", lambda *args: {"requested_uids": [7]})
    app = server.create_app(remote_host="example.com", approval_store=ApprovalStore(), multi_user=Broker())
    with TestClient(app, base_url="https://example.com") as client:
        response = client.post("/approve/preview", data={"token": "token"})
    assert response.status_code == 200
    assert "7" in response.text

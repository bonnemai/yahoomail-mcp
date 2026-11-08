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

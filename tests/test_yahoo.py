from email import policy
from email.message import EmailMessage

import pytest

from yahoomail_mcp import yahoo


class FakeConnection:
    def __init__(self, response=b"1 2"):
        self.response_data = response
        self.calls = []
        self.literal = None

    def uid(self, *args):
        self.calls.append(args)
        return "OK", [self.response_data]

    def select(self, *args, **kwargs):
        self.calls.append(("select", args, kwargs))
        return "OK", [b"1"]

    def response(self, name):
        assert name == "UIDVALIDITY"
        return "OK", [b"42"]

    def append(self, *args):
        self.calls.append(("append", args))
        return "OK", [b"saved"]

    def capability(self):
        return "OK", [b"* CAPABILITY IMAP4 MOVE"]

    def list(self):
        return "OK", [b'(\\Drafts) "" Drafts', b'(\\Trash) "" Trash']


def test_quoted_escapes_and_rejects_control_characters():
    assert yahoo.quoted('A\\B"C') == '"A\\\\B\\"C"'
    with pytest.raises(yahoo.MailError):
        yahoo.quoted("bad\nname")


def test_search_uids_supports_pagination_and_sorts_descending():
    conn = FakeConnection(b"3 1 2")
    assert yahoo.search_uids(conn, "SUBJECT", "hello", 4) == [3, 2, 1]
    assert conn.calls[-1] == ("SEARCH", None, "UID", "1:3", "SUBJECT", '"hello"')
    assert yahoo.search_uids(conn, "SUBJECT", "", None) == [3, 2, 1]


def test_search_uids_uses_utf8_literal_for_non_ascii_query():
    conn = FakeConnection(b"7")
    assert yahoo.search_uids(conn, "TEXT", "café", None) == [7]
    assert conn.literal == "café".encode()
    assert conn.calls[-1] == ("SEARCH", "CHARSET", "UTF-8", "TEXT")


def test_summary_and_read_extract_plain_text_and_attachments(monkeypatch):
    message = EmailMessage(policy=policy.default)
    message["From"] = "sender@example.com"
    message["Subject"] = "Hello"
    message["Date"] = "today"
    message.set_content("hello")
    message.add_attachment(b"data", maintype="text", subtype="plain", filename="note.txt")
    monkeypatch.setattr(yahoo, "fetch_literals", lambda conn, uids, section: {9: message.as_bytes()})
    monkeypatch.setattr(yahoo, "checked", lambda response: response[1])
    conn = FakeConnection()
    conn.uid = lambda *args: ("OK", [b"* 9 FETCH (RFC822.SIZE 20)"])

    result = yahoo.read(conn, 9)
    assert result["uid"] == 9
    assert result["subject"] == "Hello"
    assert result["body"].strip() == "hello"
    assert result["attachments"] == ["note.txt"]


def test_read_rejects_oversized_message(monkeypatch):
    monkeypatch.setattr(yahoo, "checked", lambda response: response[1])
    conn = FakeConnection()
    conn.uid = lambda *args: ("OK", [b"* 9 FETCH (RFC822.SIZE 1000001)"])
    with pytest.raises(yahoo.MailError, match="over 1 MB"):
        yahoo.read(conn, 9)


def test_append_draft_validates_recipient_and_sets_reply_headers(monkeypatch):
    monkeypatch.setenv("YAHOO_EMAIL", "me@example.com")
    conn = FakeConnection()
    parent = {"Message-ID": "<parent@example.com>", "References": "<root@example.com>"}
    result = yahoo.append_draft(conn, "you@example.com", "Re: hi", "body", parent)
    assert result["saved"] is True
    append_args = next(args for name, args in conn.calls if name == "append")
    raw = append_args[-1]
    assert b"In-Reply-To: <parent@example.com>" in raw
    assert b"References: <root@example.com> <parent@example.com>" in raw

    with pytest.raises(yahoo.MailError, match="valid recipient"):
        yahoo.append_draft(conn, "not-an-email", "subject", "body")


def test_special_folder_uses_override(monkeypatch):
    monkeypatch.setenv("YAHOO_TRASH_FOLDER", "Yahoo Trash")
    assert yahoo.special_folder(FakeConnection(), r"\\Trash", "YAHOO_TRASH_FOLDER", "Trash") == "Yahoo Trash"


def test_checked_and_select_validate_imap_responses():
    with pytest.raises(yahoo.MailError):
        yahoo.checked(("NO", [b"bad"]))
    conn = FakeConnection()
    assert yahoo.select(conn, "INBOX") == 42
    with pytest.raises(yahoo.MailError):
        yahoo.select(type("Bad", (), {"select": lambda *a, **k: ("OK", []), "response": lambda *a: ("OK", [b""])})(), "INBOX")


def test_fetch_literals_and_headers_parse_uid_attributes(monkeypatch):
    conn = FakeConnection()
    conn.uid = lambda *args: ("OK", [(b"* 1 FETCH (UID 8 BODY[] {3})", b"abc"), b")"])
    assert yahoo.fetch_literals(conn, [8], "BODY[]") == {8: b"abc"}
    raw = b"Subject: Parsed\nFrom: a@example.com\n\n"
    monkeypatch.setattr(yahoo, "fetch_literals", lambda *args: {8: raw})
    assert yahoo.headers(conn, [8])[8]["Subject"] == "Parsed"


def test_html_text_ignores_hidden_content():
    parser = yahoo.HTMLText()
    parser.feed("<p>Visible</p><script>secret</script><div>Next<br>line</div>")
    parser.close()
    assert "Visible" in "".join(parser.parts)
    assert "secret" not in "".join(parser.parts)


def test_move_to_trash_requires_move_and_validates_source(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setenv("YAHOO_TRASH_FOLDER", "Trash")
    with pytest.raises(yahoo.MailError, match="already in Trash"):
        yahoo.move_to_trash(conn, "Trash", 42, [1])
    monkeypatch.setenv("YAHOO_TRASH_FOLDER", "Deleted")
    conn.capability = lambda: ("OK", [b"* CAPABILITY IMAP4"])
    with pytest.raises(yahoo.MailError, match="MOVE"):
        yahoo.move_to_trash(conn, "INBOX", 42, [1])


def test_related_uids_uses_message_id(monkeypatch):
    conn = FakeConnection(b"4 5")
    message = {"Message-ID": "<id@example.com>"}
    assert yahoo.related_uids(conn, message) == {4, 5}
    assert "HEADER" in conn.calls[-1]
    assert yahoo.related_uids(conn, {}) == set()


def test_connection_requires_credentials(monkeypatch):
    monkeypatch.delenv("YAHOO_EMAIL", raising=False)
    monkeypatch.delenv("YAHOO_APP_PASSWORD", raising=False)
    with pytest.raises(yahoo.MailError, match="Set YAHOO_EMAIL"):
        with yahoo.connection():
            pass


def test_connection_error_reports_safe_stage_and_category():
    auth_error = yahoo.connection_error("login", yahoo.imaplib.IMAP4.error("secret server response"))
    assert "login" in auth_error
    assert "sign-in" in auth_error
    assert "secret server response" not in auth_error

    network_error = yahoo.connection_error("connection", OSError("private host detail"))
    assert "connection" in network_error
    assert "imap.mail.yahoo.com:993" in network_error
    assert "private host detail" not in network_error

from urllib.parse import parse_qs, urlparse

import pytest

from yahoomail_mcp import multi_user
from yahoomail_mcp.multi_user import MultiUserError, MultiUserStore, OAuthConfig, YahooOAuth


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[Item["record_id"]] = Item

    def get_item(self, Key, ConsistentRead=True):
        return {"Item": self.items[Key["record_id"]]} if Key["record_id"] in self.items else {}

    def delete_item(self, Key, **kwargs):
        item = self.items.get(Key["record_id"])
        if "ConditionExpression" in kwargs:
            values = kwargs["ExpressionAttributeValues"]
            if (
                not item
                or item.get("kind") != values[":kind"]
                or int(item.get("expires_at", 0)) <= values[":now"]
            ):
                from botocore.exceptions import ClientError

                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}},
                    "DeleteItem",
                )
            self.items.pop(Key["record_id"])
            return {"Attributes": item}
        self.items.pop(Key["record_id"], None)


def test_oauth_start_uses_pkce_and_persists_state():
    table = FakeTable()
    oauth = YahooOAuth(OAuthConfig("client", "secret", "https://example.test/callback"), MultiUserStore(table))

    url, state = oauth.start("/connected")
    query = parse_qs(urlparse(url).query)

    assert url.startswith(oauth.AUTHORIZATION_URL)
    assert query["client_id"] == ["client"]
    assert query["redirect_uri"] == ["https://example.test/callback"]
    assert query["state"] == [state]
    assert query["code_challenge_method"] == ["S256"]
    assert table.items[f"state:{state}"]["return_to"] == "/connected"


def test_oauth_finish_stores_user_and_returns_one_time_session(monkeypatch):
    table = FakeTable()
    store = MultiUserStore(table)
    oauth = YahooOAuth(OAuthConfig("client", "secret", "https://example.test/callback"), store)
    _, state = oauth.start()

    class Response:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    monkeypatch.setattr(
        "yahoomail_mcp.multi_user.httpx.post",
        lambda *args, **kwargs: Response({"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}),
    )
    monkeypatch.setattr(
        "yahoomail_mcp.multi_user.httpx.get",
        lambda *args, **kwargs: Response({"sub": "user-1", "email": "user@example.com"}),
    )

    session, return_to = oauth.finish("authorization-code", state)

    assert return_to == "/"
    assert len(session) > 32
    assert store.get_session(session)["user_id"] == "user-1"
    assert store.get_user("user-1")["email"] == "user@example.com"
    assert store.consume_state(state) is None


def test_store_expires_state_and_session(monkeypatch):
    table = FakeTable()
    store = MultiUserStore(table)
    monkeypatch.setattr(multi_user.time, "time", lambda: 1000)
    store.put_state("state", "verifier", "/")
    store.put_session("session", "user")
    assert store.consume_state("state")["verifier"] == "verifier"
    assert store.get_session("session")["user_id"] == "user"
    monkeypatch.setattr(multi_user.time, "time", lambda: 3_000_000)
    assert store.consume_state("state") is None
    assert store.get_session("session") is None


def test_mailbox_refreshes_an_expired_access_token(monkeypatch):
    table = FakeTable()
    store = MultiUserStore(table)
    store.put_user("user", "user@example.com", "old", "refresh", 1)
    oauth = YahooOAuth(OAuthConfig("client", "secret", "https://example.test/callback"), store)

    class Response:
        status_code = 200

        def json(self):
            return {"access_token": "new", "refresh_token": "new-refresh", "expires_in": 3600}

    monkeypatch.setattr("yahoomail_mcp.multi_user.httpx.post", lambda *args, **kwargs: Response())
    email, token = oauth.mailbox("user")
    assert (email, token) == ("user@example.com", "new")
    assert store.get_user("user")["refresh_token"] == "new-refresh"


@pytest.mark.parametrize("method", ["get_user", "get_session"])
def test_missing_user_records_return_none(method):
    assert getattr(MultiUserStore(FakeTable()), method)("missing") is None


def test_oauth_rejects_invalid_state():
    oauth = YahooOAuth(OAuthConfig("client", "secret", "https://example.test/callback"), MultiUserStore(FakeTable()))
    with pytest.raises(MultiUserError, match="invalid or expired"):
        oauth.finish("code", "missing")

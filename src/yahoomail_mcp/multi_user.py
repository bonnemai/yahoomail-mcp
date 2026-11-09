"""Yahoo OAuth and per-user session storage for the Lambda multi-user mode."""
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from botocore.exceptions import ClientError


class MultiUserError(Exception):
    pass


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scope: str = "openid email mail-r"


class MultiUserStore:
    """DynamoDB-backed records for OAuth state, users, and MCP sessions."""

    def __init__(self, table):
        self.table = table

    def put(self, record_id, item, expires_at):
        self.table.put_item(Item={"record_id": record_id, "expires_at": expires_at, **item})

    def get(self, record_id):
        return self.table.get_item(Key={"record_id": record_id}, ConsistentRead=True).get("Item")

    def delete(self, record_id):
        self.table.delete_item(Key={"record_id": record_id})

    def put_state(self, state, verifier, return_to):
        self.put(f"state:{state}", {"kind": "oauth_state", "verifier": verifier, "return_to": return_to}, int(time.time()) + 600)

    def consume_state(self, state):
        try:
            response = self.table.delete_item(
                Key={"record_id": f"state:{state}"},
                ConditionExpression="kind = :kind AND expires_at > :now",
                ExpressionAttributeValues={":kind": "oauth_state", ":now": int(time.time())},
                ReturnValues="ALL_OLD",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise
        return response.get("Attributes")

    def put_user(self, user_id, email, access_token, refresh_token, expires_at):
        self.put(
            f"user:{user_id}",
            {
                "kind": "user",
                "user_id": user_id,
                "email": email,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_expires_at": expires_at,
            },
            int(time.time()) + 31_536_000,
        )

    def get_user(self, user_id):
        return self.get(f"user:{user_id}")

    def put_session(self, session_token, user_id):
        self.put(
            f"session:{_hash(session_token)}",
            {"kind": "session", "user_id": user_id},
            int(time.time()) + 2_592_000,
        )

    def get_session(self, session_token):
        item = self.get(f"session:{_hash(session_token)}")
        if not item or int(item.get("expires_at", 0)) <= int(time.time()):
            return None
        return item


class YahooOAuth:
    AUTHORIZATION_URL = "https://api.login.yahoo.com/oauth2/request_auth"
    TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
    USERINFO_URL = "https://api.login.yahoo.com/openid/v1/userinfo"

    def __init__(self, config, store):
        self.config = config
        self.store = store

    def start(self, return_to="/"):
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        nonce = secrets.token_urlsafe(24)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.store.put_state(state, verifier, return_to if return_to.startswith("/") else "/")
        query = urlencode({
            "client_id": self.config.client_id,
            "response_type": "code",
            "redirect_uri": self.config.redirect_uri,
            "scope": self.config.scope,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        return f"{self.AUTHORIZATION_URL}?{query}", state

    def finish(self, code, state):
        state_record = self.store.consume_state(state)
        if not state_record:
            raise MultiUserError("OAuth state is invalid or expired.")
        response = httpx.post(
            self.TOKEN_URL,
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "redirect_uri": self.config.redirect_uri,
                "code": code,
                "code_verifier": state_record["verifier"],
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
        if response.status_code >= 400:
            raise MultiUserError("Yahoo rejected the OAuth authorization code.")
        tokens = response.json()
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        if not access_token or not refresh_token:
            raise MultiUserError("Yahoo did not return the required OAuth tokens.")
        profile = httpx.get(self.USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
        if profile.status_code >= 400:
            raise MultiUserError("Yahoo did not return the authorized user profile.")
        user = profile.json()
        user_id = str(user.get("sub") or user.get("xoauth_yahoo_guid") or "")
        email = str(user.get("email") or "")
        if not user_id or not email:
            raise MultiUserError("Yahoo did not return a stable user ID and email address.")
        expires_at = int(time.time()) + int(tokens.get("expires_in", 3600))
        self.store.put_user(user_id, email, access_token, refresh_token, expires_at)
        session_token = secrets.token_urlsafe(48)
        self.store.put_session(session_token, user_id)
        return session_token, state_record.get("return_to", "/")

    def mailbox(self, user_id):
        user = self.store.get_user(user_id)
        if not user:
            raise MultiUserError("Yahoo account is not connected for this user.")
        if int(user.get("token_expires_at", 0)) <= int(time.time()) + 60:
            response = httpx.post(
                self.TOKEN_URL,
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "refresh_token": user["refresh_token"],
                    "grant_type": "refresh_token",
                },
                timeout=20,
            )
            if response.status_code >= 400:
                raise MultiUserError("Yahoo OAuth refresh failed; reconnect the account.")
            tokens = response.json()
            access_token = tokens.get("access_token")
            if not access_token:
                raise MultiUserError("Yahoo did not return a refreshed access token.")
            user["access_token"] = access_token
            user["token_expires_at"] = int(time.time()) + int(tokens.get("expires_in", 3600))
            if tokens.get("refresh_token"):
                user["refresh_token"] = tokens["refresh_token"]
            self.store.put_user(
                user_id, user["email"], user["access_token"], user["refresh_token"], user["token_expires_at"]
            )
        return user["email"], user["access_token"]

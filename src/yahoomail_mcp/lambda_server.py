"""Single-user and multi-user MCP deployments for AWS Lambda."""
import json
import os

import uvicorn

from .approval import DynamoApprovalStore
from .multi_user import MultiUserStore, OAuthConfig, YahooOAuth
from .server import create_app


def configure(secret, *, table=None, user_table=None, approval_url="", mode="single-user"):
    host = os.environ["MCP_REMOTE_HOST"]
    if not host:
        raise ValueError("MCP_REMOTE_HOST is required.")
    if mode == "multi-user":
        for name in ("YAHOO_OAUTH_CLIENT_ID", "YAHOO_OAUTH_CLIENT_SECRET"):
            if not isinstance(secret.get(name), str) or not secret[name]:
                raise ValueError(f"The multi-user secret must contain a non-empty {name}.")
        if user_table is None:
            raise ValueError("The multi-user mode requires a user table.")
        store = MultiUserStore(user_table)
        broker = YahooOAuth(
            OAuthConfig(
                client_id=secret["YAHOO_OAUTH_CLIENT_ID"],
                client_secret=secret["YAHOO_OAUTH_CLIENT_SECRET"],
                redirect_uri=os.environ["YAHOO_OAUTH_REDIRECT_URI"],
            ),
            store,
        )
        os.environ["YAHOO_ENABLE_WRITES"] = "1" if table is not None else "0"
        return create_app(
            remote_host=host,
            approval_store=DynamoApprovalStore(table) if table is not None else None,
            approval_url=approval_url,
            multi_user=broker,
        )

    token = secret["YAHOO_MCP_TOKEN"]
    if not isinstance(token, str) or not token:
        raise ValueError("The Lambda secret must contain a non-empty YAHOO_MCP_TOKEN.")
    for name in ("YAHOO_EMAIL", "YAHOO_APP_PASSWORD"):
        value = secret[name]
        if not isinstance(value, str) or not value:
            raise ValueError(f"The Lambda secret must contain a non-empty {name}.")
        os.environ[name] = value
    os.environ["YAHOO_ENABLE_WRITES"] = "1" if table is not None else "0"
    return create_app(
        token,
        remote_host=host,
        approval_store=DynamoApprovalStore(table) if table is not None else None,
        approval_url=approval_url,
    )


def main():
    import boto3

    secrets_manager = boto3.client("secretsmanager")
    response = secrets_manager.get_secret_value(SecretId=os.environ["YAHOO_MCP_SECRET_ARN"])
    try:
        host = os.environ["MCP_REMOTE_HOST"]
        table = None
        user_table = None
        writes = os.environ.get("YAHOO_MCP_ENABLE_REMOTE_WRITES") == "true"
        if writes:
            table = boto3.resource("dynamodb").Table(os.environ["YAHOO_MCP_APPROVAL_TABLE"])
        if os.environ.get("YAHOO_DEPLOYMENT_MODE", "single-user") == "multi-user":
            user_table = boto3.resource("dynamodb").Table(os.environ["YAHOO_MCP_USER_TABLE"])
        app = configure(
            json.loads(response["SecretString"]),
            table=table,
            user_table=user_table,
            approval_url=os.environ.get("MCP_APPROVAL_BASE_URL", f"https://{host}/approve"),
            mode=os.environ.get("YAHOO_DEPLOYMENT_MODE", "single-user"),
        )
    except (ValueError, KeyError, TypeError):
        raise SystemExit("Invalid Yahoo MCP secret or Lambda configuration.") from None
    uvicorn.run(app, host="127.0.0.1", port=8000, access_log=False)


if __name__ == "__main__":
    main()

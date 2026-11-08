"""Single-account, MCP deployment for AWS Lambda."""
import json
import os

import uvicorn

from .approval import DynamoApprovalStore
from .server import create_app


def configure(secret, *, table=None, approval_url=""):
    token = secret["YAHOO_MCP_TOKEN"]
    if not isinstance(token, str) or not token:
        raise ValueError("The Lambda secret must contain a non-empty YAHOO_MCP_TOKEN.")
    for name in ("YAHOO_EMAIL", "YAHOO_APP_PASSWORD"):
        value = secret[name]
        if not isinstance(value, str) or not value:
            raise ValueError(f"The Lambda secret must contain a non-empty {name}.")
        os.environ[name] = value
    os.environ["YAHOO_ENABLE_WRITES"] = "1" if table is not None else "0"
    host = os.environ["MCP_REMOTE_HOST"]
    if not host:
        raise ValueError("MCP_REMOTE_HOST is required.")
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
        if os.environ.get("YAHOO_MCP_ENABLE_REMOTE_WRITES") == "true":
            table = boto3.resource("dynamodb").Table(os.environ["YAHOO_MCP_APPROVAL_TABLE"])
        app = configure(
            json.loads(response["SecretString"]),
            table=table,
            approval_url=os.environ.get("MCP_APPROVAL_BASE_URL", f"https://{host}/approve"),
        )
    except (ValueError, KeyError, TypeError):
        raise SystemExit("Invalid Yahoo MCP secret or Lambda configuration.") from None
    uvicorn.run(app, host="127.0.0.1", port=8000, access_log=False)


if __name__ == "__main__":
    main()

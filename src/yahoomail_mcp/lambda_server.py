"""Single-account, read-only MCP deployment for AWS Lambda."""
import json
import os

import uvicorn

from .server import create_app


def configure(secret):
    token = secret["YAHOO_MCP_TOKEN"]
    if not isinstance(token, str) or not token:
        raise ValueError("The Lambda secret must contain a non-empty YAHOO_MCP_TOKEN.")
    for name in ("YAHOO_EMAIL", "YAHOO_APP_PASSWORD"):
        value = secret[name]
        if not isinstance(value, str) or not value:
            raise ValueError(f"The Lambda secret must contain a non-empty {name}.")
        os.environ[name] = value
    os.environ["YAHOO_ENABLE_WRITES"] = "0"
    host = os.environ["MCP_REMOTE_HOST"]
    if not host:
        raise ValueError("MCP_REMOTE_HOST is required.")
    return create_app(token, remote_host=host)


def main():
    import boto3

    secrets_manager = boto3.client("secretsmanager")
    response = secrets_manager.get_secret_value(SecretId=os.environ["YAHOO_MCP_SECRET_ARN"])
    try:
        app = configure(json.loads(response["SecretString"]))
    except (ValueError, KeyError, TypeError):
        raise SystemExit("Invalid Yahoo MCP secret or Lambda configuration.") from None
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()

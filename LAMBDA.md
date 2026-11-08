# Authenticated Yahoo Mail MCP

Start with the local test on your Mac. The AWS deployment is a separate, single-account profile for search, message reading and thread reading. Yahoo access still uses IMAP over TLS.

## 1. Test on your Mac

From the project directory, generate a token.

```sh
openssl rand -hex 32
```

Copy it into `.env` as `YAHOO_MCP_TOKEN=your_generated_token`, alongside the Yahoo credentials. Set `YAHOO_ENABLE_WRITES=0` for this test. Restart the server using one mode from the main README. For example, use `./run.sh macos-linux` for Apple Container.

In a second terminal, send a request without the token. It should print **401**.

```sh
curl -sS -o /dev/null -w '%{http_code}\n' \
  -X POST http://127.0.0.1:8000/mcp
```

Then enter the same token in the terminal when prompted. This zsh command reads it without echoing it or putting its value in shell history.

```zsh
read -rs 'YAHOO_MCP_TOKEN?MCP bearer token: '
printf '\n'
export YAHOO_MCP_TOKEN
.venv/bin/python scripts/smoke.py --search
```

If needed, create the virtual environment and run `.venv/bin/python -m pip install -e .` as shown in the main README. The test should print `MCP ready` and `Yahoo search OK`. Local mode still lists all five tools. The write operations are disabled by the environment setting above.

To try from Codex, add a separate entry in `~/.codex/config.toml` and launch Codex from the terminal where you exported the token.

```toml
[mcp_servers.yahoo-mail-auth]
url = "http://127.0.0.1:8000/mcp"
bearer_token_env_var = "YAHOO_MCP_TOKEN"
```

Ask it to use `yahoo-mail-auth` to find the latest message in `INBOX`. If another local client starts getting 401 after you turn authentication on, configure that client to send the same bearer token.

## 2. Deploy when the local test passes

Use Docker Desktop, the AWS CLI and the AWS SAM CLI. The template targets ARM64, so build it on an Apple Silicon Mac with Docker running. Check that `aws sts get-caller-identity` shows the account you intend to deploy into.

In **Secrets Manager**, create a secret in **Europe (London), `eu-west-2`** or your chosen region. Use the default Secrets Manager encryption key. Store these three keys in one JSON secret and copy its ARN. Use a fresh token for AWS, separate from the local one.

| Key | Value |
|---|---|
| `YAHOO_EMAIL` | Your Yahoo address |
| `YAHOO_APP_PASSWORD` | Your Yahoo app password |
| `YAHOO_MCP_TOKEN` | A token generated with `openssl rand -hex 32` |

The Lambda role can read this secret. SAM receives the ARN only.

```sh
sam validate --lint --region eu-west-2
sam build
sam deploy --stack-name yahoo-mail-mcp --region eu-west-2 \
  --resolve-image-repos --capabilities CAPABILITY_IAM \
  --confirm-changeset \
  --parameter-overrides YahooMcpSecretArn=YOUR_SECRET_ARN
```

Review the change set. Copy the **McpUrl** output. Set it as `MCP_URL` in the terminal, repeat the local unauthenticated curl test with that URL, then export the **AWS** bearer token and run:

```sh
.venv/bin/python scripts/smoke.py --url "$MCP_URL" --read-only --search
```

For Codex, create another config entry using the HTTPS URL and the same `bearer_token_env_var` setting. Use only a client that can send a preconfigured bearer token. This endpoint has no interactive OAuth flow or Yahoo account switching.

The API Gateway URL is internet-reachable. Every MCP request must supply the bearer token, which grants read access to this mailbox. The endpoint exposes three read tools and Lambda runs with writes disabled. Keep the token private and use only HTTPS for remote connections. Lambda reads the secret on cold start, so existing execution environments can temporarily keep old values after rotation. For immediate revocation, disable the endpoint and recycle the function through a deployment before restoring access. To remove the SAM stack, run `sam delete --stack-name yahoo-mail-mcp --region eu-west-2`. The separately created secret must be managed separately.

The gateway uses buffered responses and a 29-second integration timeout. Very large or slow IMAP searches may hit that deadline. Check Lambda logs for startup or IMAP errors. The server does not log the bearer token or message content during normal operation.

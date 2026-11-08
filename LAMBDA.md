# AWS Lambda Deployment

This document covers the remote deployment only. For server installation, Yahoo setup, Apple Container/Docker, and local testing, follow [README.md](./README.md).

## Architecture

```text
MCP client --Bearer token--> API Gateway HTTP API
                                  |
                                  v
                         Lambda + Web Adapter
                           |              |
                           v              v
                    Yahoo IMAP/TLS     DynamoDB
                                      TTL previews
```

Lambda uses a bearer token separate from the local token. Yahoo credentials and this token are read from Secrets Manager. The deployment is read-only by default. With `EnableRemoteWrites=true`, writes are limited to moving messages to Trash after explicit approval; drafts and sending are not exposed by the remote profile.

The `/approve/...` page displays the preview and requires a user click. Approvals do not require checking CloudWatch Logs. Each preview is single-use and expires after five minutes. The approval link is itself a secret: do not publish it in a ticket, log, or shared conversation.

## Prerequisites

- AWS CLI configured for the target account.
- AWS SAM CLI.
- Docker Desktop running.
- An Apple Silicon Mac for the ARM64 image.
- A Yahoo account with an app password created as described in [README.md](./README.md).

Check the account and region:

```sh
aws sts get-caller-identity
aws configure get region
```

The examples use `eu-west-2` (London). Replace this region everywhere if needed.

## 1. Create the Yahoo secret

Generate a remote token separate from the local token:

```sh
openssl rand -hex 32
```

Create a JSON secret in Secrets Manager, for example using a protected temporary file:

```json
{
  "YAHOO_EMAIL": "your-address@yahoo.com",
  "YAHOO_APP_PASSWORD": "your_yahoo_app_password",
  "YAHOO_MCP_TOKEN": "your_remote_token"
}
```

Do not send the app password or token in an AI prompt. Keep the secret in Secrets Manager; it is never copied into the CloudFormation template.

```sh
aws secretsmanager create-secret \
  --name yahoo-mail-mcp/account \
  --region eu-west-2 \
  --secret-string file://yahoo-secret.json
```

Record the returned ARN (`ARN`). Then securely remove the local file containing the secret.

## 2. Validate and deploy

From the project root:

```sh
sam validate --lint --region eu-west-2
sam build --use-container
sam deploy --stack-name yahoo-mail-mcp \
  --region eu-west-2 \
  --resolve-image-repos \
  --capabilities CAPABILITY_IAM \
  --confirm-changeset \
  --parameter-overrides YahooMcpSecretArn=YOUR_SECRET_ARN
```

The stack creates:

- an API Gateway HTTP API with `/mcp` and, when writes are enabled, `/approve/{approval_id}`;
- an ARM64 Lambda based on [Dockerfile.lambda](./Dockerfile.lambda);
- an on-demand DynamoDB table with preview expiration, when writes are enabled;
- an execution role allowed to read the secret and, when writes are enabled, use the approval table;
- a log group retained for seven days.

Accept the change set only after checking the account, region, and secret ARN.

## 3. Verify the endpoint

Retrieve the URL:

```sh
MCP_URL=$(aws cloudformation describe-stacks \
  --stack-name yahoo-mail-mcp \
  --region eu-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`McpUrl`].OutputValue' \
  --output text)
printf '%s\n' "$MCP_URL"
```

Without a token, the response should be `401`:

```sh
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$MCP_URL"
```

In a terminal that will not be shared, load the token without displaying it:

```zsh
read -rs 'YAHOO_MCP_TOKEN?Remote MCP bearer token: '
printf '\n'
export YAHOO_MCP_TOKEN MCP_URL
.venv/bin/python scripts/smoke.py --url "$MCP_URL" --read-only --search
```

The test should discover the read and analysis tools, then print `Yahoo search OK`.

## 4. Use remote cleanup

After validating read-only mode, deploy again with the same parameters and add `EnableRemoteWrites=true` to `--parameter-overrides`. The browser route uses the link token, while `/mcp` still requires the bearer token.

In the authenticated MCP client:

1. Call `analyze_senders` to identify high-volume or large mailing lists.
2. Call `unsubscribe_options` to show only standard `List-Unsubscribe` options.
3. Call `suggest_cleanup` to obtain candidates; this step changes nothing.
4. Explicitly select the UIDs to move and call `preview_cleanup`.
5. Open the returned `approval_url` in a browser, verify the subjects and senders, and click **Approve move to Trash**.

The approval is consumed before contacting Yahoo. If an error or uncertain response occurs, check Yahoo Mail before creating a new preview. Lambda then moves the messages through IMAP `MOVE`. Nothing is permanently deleted. If Yahoo does not advertise `MOVE`, the interface will instruct you to perform the operation in Yahoo Mail.

Do not ask the AI to follow the approval URL automatically. The click must be a human action after reviewing the preview.

## 5. Configure an MCP client

The remote endpoint uses a preconfigured bearer token. Example:

```toml
[mcp_servers.yahoo-mail-aws]
url = "https://YOUR_API_ID.execute-api.eu-west-2.amazonaws.com/mcp"
bearer_token_env_var = "YAHOO_MCP_TOKEN"
```

This is shared-secret authentication, not a complete OAuth flow. For enterprise OAuth, put Cognito or another OAuth 2.0 provider in front of API Gateway and replace application-level bearer validation with JWT/API Gateway Authorizer validation.

## Troubleshooting and removal

Check logs only for startup, IMAP, or AWS errors:

```sh
sam logs --stack-name yahoo-mail-mcp --region eu-west-2 --tail
```

Also verify that the secret exists in the same region, that its ARN is correct, and that Yahoo accepts the app password.

To delete the stack:

```sh
sam delete --stack-name yahoo-mail-mcp --region eu-west-2
```

The separately created secret must be deleted or retained separately; the stack does not delete it.

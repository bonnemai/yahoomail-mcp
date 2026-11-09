# AWS Lambda Multi-User Deployment

The default project mode is single-user local mode. Follow [README.md](./README.md) for local setup, Yahoo app passwords, Docker/Apple Container, and local testing.

This guide covers the separate AWS multi-user mode. Each person authorizes their own Yahoo mailbox with Yahoo OAuth. No Yahoo app password is used in this mode.

## Architecture

```text
User --Yahoo OAuth--> /oauth/start and /oauth/callback
  |                                      |
  | receives a per-user MCP token        v
  +------------> API Gateway --> Lambda + Web Adapter
                                  |       |
                                  v       v
                           Yahoo IMAP   DynamoDB
                           XOAUTH2      users/sessions
                                      approvals (optional)
```

The MCP session token identifies a user. It does not contain a Yahoo password, access token, or refresh token. Lambda looks up that user's OAuth record and opens the IMAP connection for that mailbox only.

## Prerequisites

- AWS CLI configured for the target account.
- AWS SAM CLI.
- Docker Desktop running.
- An Apple Silicon Mac for the ARM64 image.
- A Yahoo Developer application with OAuth enabled for Mail access.

AWS SAM (Serverless Application Model) CLI is the command-line tool used here to validate, build, and deploy the `template.yaml` CloudFormation stack. It packages the Lambda container and creates the API Gateway, DynamoDB tables, IAM permissions, and log group described by the template. Install it from the [AWS SAM CLI installation guide](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).

The Yahoo OAuth application needs the callback URL produced by the stack:

```text
https://YOUR_API_ID.execute-api.eu-west-2.amazonaws.com/oauth/callback
```

Yahoo's OAuth authorization and token endpoints are documented in the [Yahoo OAuth 2.0 guide](https://developer.yahoo.com/oauth2/guide/). Yahoo Mail access is not automatically available to every application: request the required Mail developer access before deploying this mode, as described by [Yahoo Developer Access](https://senders.yahooinc.com/developer/developer-access/).

## 1. Create the OAuth secret

Create a Secrets Manager JSON secret containing only the OAuth application credentials:

```json
{
  "YAHOO_OAUTH_CLIENT_ID": "your_yahoo_client_id",
  "YAHOO_OAUTH_CLIENT_SECRET": "your_yahoo_client_secret"
}
```

Do not put a user's email address, Yahoo app password, access token, or refresh token in this secret. User OAuth tokens are stored in the per-user DynamoDB table.

```sh
aws secretsmanager create-secret \
  --name yahoo-mail-mcp/oauth \
  --region eu-west-2 \
  --secret-string file://yahoo-oauth-secret.json
```

Record the returned ARN and remove the local JSON file securely.

## 2. Deploy read-only multi-user mode

From the project root:

```sh
aws sts get-caller-identity
sam validate --lint --region eu-west-2
sam build --use-container
sam deploy --stack-name yahoo-mail-mcp \
  --region eu-west-2 \
  --resolve-image-repos \
  --capabilities CAPABILITY_IAM \
  --confirm-changeset \
  --parameter-overrides \
    YahooMcpSecretArn=YOUR_SECRET_ARN \
    DeploymentMode=multi-user \
    EnableRemoteWrites=false
```

The stack creates:

- API Gateway routes for `/mcp`, `/oauth/start`, and `/oauth/callback`;
- an ARM64 Lambda based on [Dockerfile.lambda](./Dockerfile.lambda);
- a DynamoDB table for users, OAuth state, and MCP sessions;
- IAM permissions for the Lambda to read the OAuth secret and user table;
- a seven-day Lambda log group.

The user table uses DynamoDB encryption at rest and TTL for OAuth state/session expiry. OAuth refresh tokens remain server-side and are never returned by an MCP tool.

## 3. Register the callback and connect a user

Get the API URL:

```sh
MCP_URL=$(aws cloudformation describe-stacks \
  --stack-name yahoo-mail-mcp \
  --region eu-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`McpUrl`].OutputValue' \
  --output text)
OAUTH_URL="${MCP_URL%/mcp}/oauth/start"
printf '%s\n' "$MCP_URL" "$OAUTH_URL"
```

Add this callback URL to the Yahoo Developer application, then open the OAuth URL in a browser:

```sh
open "$OAUTH_URL"
```

After the user approves Mail access, the callback page displays a per-user MCP session token once. Copy it into the MCP client environment without putting it in a shared chat or source-control file.

Example client configuration:

```toml
[mcp_servers.yahoo-mail-aws]
url = "https://YOUR_API_ID.execute-api.eu-west-2.amazonaws.com/mcp"
bearer_token_env_var = "YAHOO_MCP_SESSION_TOKEN"
```

The user must set `YAHOO_MCP_SESSION_TOKEN` in the client process before connecting. A session token grants access only to the Yahoo mailbox authorized by that user.

## 4. Verify the connection

With the per-user session token exported:

```sh
export YAHOO_MCP_SESSION_TOKEN
MCP_URL="https://YOUR_API_ID.execute-api.eu-west-2.amazonaws.com/mcp"
.venv/bin/python scripts/smoke.py --url "$MCP_URL" --read-only --search
```

The Lambda multi-user profile exposes search, reading, sender analysis, cleanup suggestions, unsubscribe inspection, and setup guidance. It does not expose drafts or direct writes while `EnableRemoteWrites=false`.

## 5. Enable reviewed cleanup

After read-only testing succeeds, deploy the same stack with:

```sh
sam deploy --stack-name yahoo-mail-mcp \
  --region eu-west-2 \
  --resolve-image-repos \
  --capabilities CAPABILITY_IAM \
  --confirm-changeset \
  --parameter-overrides \
    YahooMcpSecretArn=YOUR_SECRET_ARN \
    DeploymentMode=multi-user \
    EnableRemoteWrites=true
```

This adds a separate DynamoDB approval table and the `/approve/{approval_id}` route. The cleanup workflow is:

1. Call `analyze_senders`.
2. Call `suggest_cleanup`.
3. Select exact message UIDs and call `preview_cleanup`.
4. Open the returned `approval_url` in a browser.
5. Verify the preview and click **Approve move to Trash**.

The approval URL is a one-time secret link and expires after five minutes. Do not ask the AI to open or submit it. Messages are moved to Trash; they are not permanently deleted.

## Security model

- Yahoo passwords are not used or stored in multi-user mode.
- OAuth application credentials remain in Secrets Manager.
- User refresh tokens remain in DynamoDB and are never exposed through MCP tools.
- The MCP session token identifies a user but does not contain mailbox credentials.
- A user can access only the mailbox associated with their own session record.
- CloudWatch logs are for diagnostics only and are not part of the approval workflow.
- The current session-token implementation is a server-issued bearer session, not a general-purpose OAuth access token for MCP clients.

## Troubleshooting and removal

Check logs only for startup, OAuth, IMAP, or AWS errors:

```sh
sam logs --stack-name yahoo-mail-mcp --region eu-west-2 --tail
```

If OAuth callback fails, verify the callback URL matches exactly in the Yahoo Developer application and that the secret is in `eu-west-2`. If a session expires, run the OAuth start URL again; the stored Yahoo refresh token is used to renew the IMAP access token when possible.

To delete the stack:

```sh
sam delete --stack-name yahoo-mail-mcp --region eu-west-2
```

The separately created Secrets Manager secret must be deleted or retained separately; the stack does not delete it.

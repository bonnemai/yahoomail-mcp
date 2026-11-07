# Yahoo Mail MCP

Private MCP server for a Yahoo Mail account. It runs on a Mac with Docker Compose or Apple Container, using the same Dockerfile in both cases. The server connects to Yahoo over IMAP/TLS and exposes its tools at **`http://127.0.0.1:8000/mcp`**. No model API key or local database is required.

## Available tools

| Tool | Action |
|---|---|
| `search_mail` | Search a folder by sender, recipient, subject, or text. Returns up to 20 results per page with useful headers. |
| `read_message` | Read a message without marking it as read. |
| `read_thread` | Read up to five related messages in the requested folder. |
| `save_draft` | Save a new draft or reply in Yahoo Mail. |
| `trash_messages` | Preview up to 20 messages, then move them to the trash using a one-time approval code. |

There is no SMTP sending or permanent deletion. Write operations are disabled by default.

## 1. Configure the Yahoo account

Open a terminal in the project directory.

```sh
cd ~/Workspaces/yahoomail-mcp
cp -n .env.example .env
chmod 600 .env
open -e .env
```

### Required: create a Yahoo app password

This server cannot log in with your regular Yahoo account password. You must create a dedicated **app password** first:

1. Open [Yahoo account security](https://login.yahoo.com/account/security).
2. Open **Generate app password** (or **Manage app passwords**).
3. Create a password for this application and copy it immediately.
4. Use that generated value as `YAHOO_APP_PASSWORD` below.

See Yahoo's [app-password instructions](https://help.yahoo.com/kb/SLN15241.html) for details. Then add your values to `.env` in `KEY=VALUE` format:

```dotenv
YAHOO_EMAIL=your-address@yahoo.com
YAHOO_APP_PASSWORD=your_app_password
YAHOO_ENABLE_WRITES=0
```

Use your full Yahoo address and the generated app password, never your normal account password. Keep `.env` on your Mac. It is excluded from Git and the Docker build context.

## 2. Start the server

Choose one runtime because all modes use port 8000.

### Option A: Docker Desktop

Start Docker Desktop, then run these commands from the project directory:

```sh
docker compose up --build -d
docker compose logs -f mcp
```

Press `Ctrl+C` to stop following the logs. Stop the server with:

```sh
docker compose down
```

### Option B: Apple Container

Install a recent version of [Apple Container](https://github.com/apple/container/releases) on an Apple Silicon Mac. This setup targets macOS 26 or later and a CLI version that supports publishing a port to a host address. Check that `container run --help` shows `--publish` with the format `[host-ip:]host-port:container-port`.

```sh
container system start
container build --tag yahoo-mail-mcp:local .
container run --detach --name yahoo-mail-mcp --env-file .env \
  --publish 127.0.0.1:8000:8000 yahoo-mail-mcp:local
container logs yahoo-mail-mcp
```

Stop and remove the container with `container stop yahoo-mail-mcp` followed by `container rm yahoo-mail-mcp`. To recreate it, run `container run` again. If your CLI cannot publish to `127.0.0.1`, use Docker Compose or update Apple Container.

### Cross-platform launcher

The project also includes [`run.sh`](./run.sh):

```sh
./run.sh local       # local Python
./run.sh docker      # Docker Compose
./run.sh macos-linux # Apple Container Linux VM
```

The local mode uses `.venv` when it is valid and falls back to an available system Python interpreter.

## 3. Verify MCP and Yahoo

These commands run on the Mac with Python 3.11 or newer. The Docker image uses Python 3.13.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python scripts/smoke.py
.venv/bin/python scripts/smoke.py --search
```

The first smoke test opens an MCP session and verifies all five tools. It works without Yahoo access. The second performs a real read-only search in `INBOX`; it does not print message contents or modify mail.

## 4. Connect an MCP client

On the Mac, use `http://127.0.0.1:8000/mcp` in a client that supports **Streamable HTTP**. For example, with Claude Code:

```sh
claude mcp add --transport http yahoo-mail http://127.0.0.1:8000/mcp
```

In ChatGPT, the local URL can be reached through the [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels), if your account and workspace have access.

1. Create a tunnel in the OpenAI platform settings and install `tunnel-client` on the Mac.
2. Follow the guide to configure an HTTP profile with `--mcp-server-url http://127.0.0.1:8000/mcp`. Keep the OpenAI tunnel key outside this project.
3. Run `tunnel-client doctor --profile PROFILE_NAME --explain`, then `tunnel-client run --profile PROFILE_NAME`.
4. In ChatGPT, create the private developer-mode app using the MCP URL provided by OpenAI. The server and tunnel must remain running while you use it.

Check [supported plans and write permissions](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt) before configuring the tunnel. OpenAI documents MCP write support for Business, Enterprise, and Edu workspaces. On Plus, start with local testing and a compatible MCP client.

The Mac port is bound to `127.0.0.1`. This is a single-user server that trusts clients able to reach that port; do not expose it directly to the Internet. Tool responses are sent to the selected MCP client. The Yahoo password is used only for the server's IMAP connection.

## 5. Test drafts and trash

Set `YAHOO_ENABLE_WRITES=1` in `.env`, then recreate the container:

- **Docker:** `docker compose up -d --force-recreate`
- **Apple Container:** stop and remove the container, then run the `container run` command above again.

### Drafts

Ask the assistant to show you a draft, then save it only after you approve it. Confirm that it can be edited in Yahoo's Drafts folder. If the connection drops during saving, check Drafts before retrying to avoid creating a duplicate.

### Trash

Start with an easy-to-identify test message.

1. Ask the assistant to find the message and show the trash preview. Check the sender, subject, and count.
2. In `docker compose logs --tail 50 mcp` or `container logs yahoo-mail-mcp`, find the `Trash preview` line with the same `preview_id`. Give the approval code to the assistant only if the preview is correct.
3. Confirm the message is in Yahoo's Trash folder, then restore it in Yahoo to validate the workflow.

An approval code expires after five minutes and can be used only once. If the container restarts, create a new preview. The server moves only the IDs from that preview using IMAP `MOVE`. If Yahoo does not advertise `MOVE`, perform the move in Yahoo Mail instead.

## Large mailboxes and troubleshooting

- **Search:** Yahoo searches within the selected folder. IMAP may return all matching IDs, but the server fetches headers only for the requested page and sends only that page to the assistant. For a large mailbox, start with a precise `FROM` or `SUBJECT` query. Results are ordered by descending folder UID.
- **Pagination:** Reuse the same folder and criteria. Pass `next_before_uid` as `before_uid`, together with the previous page's `uid_validity`. To read a result, reuse its `uid` and the same folder references.
- **Reading:** Version 1 limits message retrieval to 1 MB and returns at most 12,000 characters of text. Attachments may be included in the MIME message fetched from Yahoo, but only their filenames are sent to the client. Threads are reconstructed from reply headers within one folder and may be partial.
- **Yahoo connection errors:** Check the app password, full email address, and network access to `imap.mail.yahoo.com:993`. If you changed `.env`, recreate the container.
- **Missing folders:** Search uses `INBOX` by default. For writes, the server looks for IMAP `\\Drafts` and `\\Trash` attributes, then tries `Draft` and `Trash`. If needed, set `YAHOO_DRAFTS_FOLDER` and `YAHOO_TRASH_FOLDER` to your account's IMAP folder names.
- **Port already in use:** Stop the server in the other runtime, then retry.
- **Local MCP works but the tunnel fails:** Check that the tunnel client is running on the Mac, targets port 8000, and passes `doctor`.

## Project structure

| Path | Purpose |
|---|---|
| `src/yahoomail_mcp/server.py` | MCP tools and trash-approval validation. |
| `src/yahoomail_mcp/yahoo.py` | IMAP connection, search, reading, drafts, and moving messages. |
| `Dockerfile` | Image used by Docker and Apple Container. |
| `compose.yaml` | Local Docker startup configuration. |
| `scripts/smoke.py` | MCP transport check and optional read-only Yahoo test. |
| `tests/` | Network-free unit tests. |
| `.env.example` | Example configuration. |

## Tests

Install the development dependencies and run the suite with coverage:

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest --cov --cov-report=term-missing
```

The coverage gate is 80% for the package. The tests mock IMAP and cover validation, pagination, MIME parsing, draft headers, and the two-step trash-approval flow. They never modify a mailbox.

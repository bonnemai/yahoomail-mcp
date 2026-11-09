"""Yahoo Mail operations over IMAP. No local mailbox cache."""
import imaplib
import logging
import os
import re
import ssl
from contextlib import contextmanager
from contextvars import ContextVar
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid
from html.parser import HTMLParser

HOST = "imap.mail.yahoo.com"
MAX_MESSAGE_BYTES = 1_000_000
BODY_LIMIT = 12_000
HEADER_FIELDS = (
    "DATE FROM TO SUBJECT MESSAGE-ID REFERENCES IN-REPLY-TO "
    "LIST-ID LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST"
)


class MailError(Exception):
    pass


current_mailbox = ContextVar("current_mailbox", default=None)


@contextmanager
def use_mailbox(email, access_token):
    marker = current_mailbox.set((email, access_token))
    try:
        yield
    finally:
        current_mailbox.reset(marker)


def connection_error(stage, exc):
    """Return a useful, non-sensitive explanation for an IMAP failure."""
    if isinstance(exc, (TimeoutError, ssl.SSLError)):
        detail = "The TLS connection to Yahoo timed out or could not be established."
    elif isinstance(exc, OSError):
        detail = "The network connection to imap.mail.yahoo.com:993 failed."
    elif stage == "login":
        detail = "Yahoo rejected the IMAP sign-in; check the full email address and app password."
    else:
        detail = "Yahoo rejected or interrupted an IMAP command."
    return f"Yahoo IMAP failed during {stage}: {detail} ({type(exc).__name__})"


@contextmanager
def connection():
    mailbox = current_mailbox.get()
    email = mailbox[0] if mailbox else os.environ.get("YAHOO_EMAIL")
    password = None if mailbox else os.environ.get("YAHOO_APP_PASSWORD")
    if not email or (not mailbox and not password):
        raise MailError("Set YAHOO_EMAIL and YAHOO_APP_PASSWORD before calling Yahoo.")
    stage = "connection"
    try:
        with imaplib.IMAP4_SSL(HOST, 993, ssl_context=ssl.create_default_context(), timeout=20) as conn:
            stage = "login"
            if mailbox:
                access_token = mailbox[1]
                auth_string = f"user={email}\1auth=Bearer {access_token}\1\1".encode()
                conn.authenticate("XOAUTH2", lambda _: auth_string)
            else:
                conn.login(email, password)
            stage = "mailbox operation"
            yield conn
    except (imaplib.IMAP4.error, OSError) as exc:
        logging.warning("Yahoo IMAP failed during %s (%s)", stage, type(exc).__name__)
        raise MailError(connection_error(stage, exc)) from None


def checked(response):
    status, data = response
    if status != "OK":
        raise MailError("Yahoo rejected the IMAP operation. For a write, check Yahoo Mail before retrying.")
    return data


def quoted(value):
    if not value.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise MailError("Use an ASCII IMAP folder name without control characters.")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def select(conn, folder, *, readonly=True):
    checked(conn.select(quoted(folder), readonly=readonly))
    _, data = conn.response("UIDVALIDITY")
    if not data or not data[0] or not data[0].isdigit():
        raise MailError("Yahoo did not return UIDVALIDITY for this folder.")
    return int(data[0])


def require_validity(conn, folder, expected, *, readonly=True):
    if select(conn, folder, readonly=readonly) != expected:
        raise MailError("The folder's message IDs changed. Search again before continuing.")


def special_folder(conn, flag, env_name, fallback):
    override = os.environ.get(env_name)
    if override:
        return override
    for row in checked(conn.list()):
        if not isinstance(row, bytes):
            continue
        match = re.fullmatch(rb'\(([^)]*)\)\s+(?:"(?:\\.|[^"])*"|NIL)\s+(.+)', row)
        if match and flag.lower().encode() in match[1].lower().split():
            name = match[2]
            if name.startswith(b'"') and name.endswith(b'"'):
                name = re.sub(rb'\\(["\\])', rb'\1', name[1:-1])
            return name.decode("ascii")
    return fallback


def search_uids(conn, field, query, before_uid):
    args = []
    if before_uid:
        if before_uid <= 1:
            return []
        args.extend(("UID", f"1:{before_uid - 1}"))
    if not query:
        data = checked(conn.uid("SEARCH", None, *args, "ALL"))
    elif query.isascii():
        data = checked(conn.uid("SEARCH", None, *args, field, quoted(query)))
    else:
        conn.literal = query.encode("utf-8")
        data = checked(conn.uid("SEARCH", "CHARSET", "UTF-8", *args, field))
    return sorted(map(int, (data[0] or b"").split()), reverse=True)


def fetch_literals(conn, uids, section):
    data = checked(conn.uid("FETCH", ",".join(map(str, uids)), f"(UID {section})"))
    results = {}
    for index, item in enumerate(data):
        if isinstance(item, tuple):
            # The server may return UID before or after the literal.
            trailer = data[index + 1] if index + 1 < len(data) else None
            attributes = item[0] + (trailer if isinstance(trailer, bytes) else b"")
            match = re.search(rb'\bUID (\d+)\b', attributes)
            if match:
                results[int(match[1])] = item[1]
    return results


def headers(conn, uids):
    if not uids:
        return {}
    literals = fetch_literals(conn, uids, f"BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})]")
    return {uid: BytesParser(policy=policy.default).parsebytes(raw) for uid, raw in literals.items()}


def metadata(conn, uids):
    """Return message size and read state without fetching message bodies."""
    if not uids:
        return {}
    data = checked(conn.uid("FETCH", ",".join(map(str, uids)), "(UID RFC822.SIZE FLAGS)"))
    result = {}
    for row in data:
        if not isinstance(row, bytes):
            continue
        uid_match = re.search(rb"\bUID (\d+)\b", row)
        size_match = re.search(rb"\bRFC822\.SIZE (\d+)\b", row)
        flags_match = re.search(rb"\bFLAGS \(([^)]*)\)", row)
        if uid_match:
            flags = flags_match[1].split() if flags_match else []
            result[int(uid_match[1])] = {
                "size": int(size_match[1]) if size_match else 0,
                "unread": b"\\Seen" not in flags,
            }
    return result


def unsubscribe_options(message):
    """Extract only standard unsubscribe values from message headers."""
    raw = str(message.get("List-Unsubscribe", ""))
    values = re.findall(r"<([^<>]+)>", raw)
    return [value for value in values if value.startswith(("https://", "mailto:"))]


def summary(uid, message):
    return {
        "uid": uid,
        **{key.lower(): str(message.get(key, ""))[:500]
           for key in ("From", "To", "Subject", "Date")},
        "list_id": str(message.get("List-ID", ""))[:300],
        "unsubscribe": unsubscribe_options(message),
    }


class HTMLText(HTMLParser):
    """Extract visible text without rendering HTML or loading remote content."""

    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = set()

    def handle_starttag(self, tag, attrs):
        if tag in {"head", "script", "style"}:
            self.hidden.add(tag)
        elif not self.hidden and tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        self.hidden.discard(tag)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

def read(conn, uid):
    data = checked(conn.uid("FETCH", str(uid), "(RFC822.SIZE)"))
    size = next((match for row in data if isinstance(row, bytes)
                 if (match := re.search(rb'\bRFC822.SIZE (\d+)', row))), None)
    if size is None:
        raise MailError("The message is no longer in this folder.")
    if int(size[1]) > MAX_MESSAGE_BYTES:
        raise MailError("This message is over 1 MB. Open it in Yahoo Mail.")
    raw = fetch_literals(conn, [uid], "BODY.PEEK[]").get(uid)
    if raw is None:
        raise MailError("The message is no longer in this folder.")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    plain = message.get_body(preferencelist=("plain",))
    html = message.get_body(preferencelist=("html",)) if plain is None else None
    body = plain.get_content() if plain else ""
    if html is not None:
        parser = HTMLText()
        parser.feed(html.get_content())
        parser.close()
        body = "".join(parser.parts).strip()
    return {
        **summary(uid, message),
        "body": body[:BODY_LIMIT],
        "truncated": len(body) > BODY_LIMIT,
        "html_only": html is not None,
        "attachments": [str(part.get_filename())[:200] for part in message.iter_attachments()
                        if part.get_filename()][:20],
    }


def append_draft(conn, to, subject, body, reply_to=None):
    folder = special_folder(conn, r"\Drafts", "YAHOO_DRAFTS_FOLDER", "Draft")
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = os.environ["YAHOO_EMAIL"]
    message["To"] = to
    recipients = message["To"]
    if recipients.defects or not recipients.addresses or any(not address.domain for address in recipients.addresses):
        raise MailError("Provide valid recipient email addresses.")
    message["Subject"] = subject
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = make_msgid()
    if reply_to and reply_to.get("Message-ID"):
        parent = str(reply_to["Message-ID"])
        message["In-Reply-To"] = parent
        message["References"] = " ".join(filter(None, (str(reply_to.get("References", "")), parent)))
    message.set_content(body)
    checked(conn.append(quoted(folder), r"(\Draft)", None, message.as_bytes()))
    return {"folder": folder, "message_id": str(message["Message-ID"]), "saved": True}


def move_to_trash(conn, folder, uid_validity, uids):
    trash = special_folder(conn, r"\Trash", "YAHOO_TRASH_FOLDER", "Trash")
    # Compare the canonical mailbox names as reported by Yahoo, and block Trash as a source.
    if trash.casefold() == folder.casefold():
        raise MailError("Messages already in Trash cannot be moved by this tool.")
    capabilities = checked(conn.capability())
    if b"MOVE" not in b" ".join(capabilities).upper().split():
        raise MailError("Yahoo did not advertise IMAP MOVE. Use Yahoo Mail for this move.")
    require_validity(conn, folder, uid_validity, readonly=False)
    if set(headers(conn, uids)) != set(uids):
        raise MailError("Some messages are no longer present. Preview again.")
    checked(conn.uid("MOVE", ",".join(map(str, uids)), quoted(trash)))
    return {"folder": trash, "requested_uids": uids, "move_accepted": True}


def related_uids(conn, message):
    # Prefer the root Message-ID from References, falling back to the parent or seed.
    for field in ("References", "In-Reply-To", "Message-ID"):
        match = re.search(r"<[^<>\s]+>", str(message.get(field, "")))
        if match and match[0].isascii():
            root = quoted(match[0])
            data = checked(conn.uid("SEARCH", None, "OR", "HEADER", "Message-ID", root,
                                    "OR", "HEADER", "References", root,
                                    "HEADER", "In-Reply-To", root))
            return set(map(int, (data[0] or b"").split()))
    return set()

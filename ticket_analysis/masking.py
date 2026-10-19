"""Secret masking applied before any text leaves the machine, and the delimiter-tag guard for prompts."""

import re

REDACTED = "[REDACTED]"
_SECRET_PATTERNS = [
    # PEM private key blocks (RSA, EC, OPENSSH, ENCRYPTED, ...)
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S),
     "[REDACTED PRIVATE KEY]"),
    # Credentials embedded in a URL: scheme://user:password@host
    (re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^\s:/@]+):[^\s@/]+@", re.I), rf"\1:{REDACTED}@"),
    # Bearer tokens (16+ characters, so ordinary prose such as "the bearer of" is left alone)
    (re.compile(r"\b(bearer)\s+[A-Za-z0-9\-._~+/]{16,}=*", re.I), rf"\1 {REDACTED}"),
    # Passcode carried in a Teams meeting join link
    (re.compile(r"(teams\.microsoft\.com/meet/\d+\?p=)[A-Za-z0-9]+", re.I), rf"\1{REDACTED}"),
    # key=value and key: value secrets, including connection-string keys and meeting passcodes
    (re.compile(r"""\b(password|passwd|pwd|passcode|pin|accountkey|sharedaccesskey|client_secret)(["']?\s*[=:]\s*)"""
                r"""("[^"]*"|'[^']*'|\{[^}]*\}|[^\s;,"']+)""", re.I), rf"\1\2{REDACTED}"),
]


def mask_secrets(text: str) -> tuple[str, int]:
    """Mask obvious secrets. Returns the masked text and the number of masks applied."""
    total = 0
    for pattern, replacement in _SECRET_PATTERNS:
        text, n = pattern.subn(replacement, text)
        total += n
    return text, total


_PROMPT_TAGS = re.compile(r"<(/?)(ticket|subject|description|client_servers)\b", re.I)
PROMPT_TAG_REPLACEMENT = r"<\1 \2"


def prepare_ticket_text(ticket: dict) -> tuple[str, str, int]:
    """Mask secrets and break any of our delimiter tags inside the ticket text.
    Returns (subject, description, secrets_masked)."""
    subject, n1 = mask_secrets(ticket.get("subject") or "")
    description, n2 = mask_secrets(ticket.get("description") or "")
    subject = _PROMPT_TAGS.sub(PROMPT_TAG_REPLACEMENT, subject)
    description = _PROMPT_TAGS.sub(PROMPT_TAG_REPLACEMENT, description)
    return subject, description, n1 + n2

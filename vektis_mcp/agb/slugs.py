"""Vestiging URL slugs: the AGB-code as ``hex(base64(code))``.

Search rows blank out the AGB-code column for vestigingen (`-`) and address the
detail page by the slug instead, so the code only exists inside the URL. Detail
pages never display a vestiging's own code either. Both URL forms exist live and
neither redirects to the other (docs/agb-source-notes.md, section 3):

    https://www.vektis.nl/agb-register/vestiging-71999001
    https://www.vektis.nl/agb-register/vestiging-4e7a45354f546b774d44453d

Decoding is strict on purpose: a code that does not come back as exactly eight
ASCII digits is a `ParseError`, never a padded or truncated guess.
"""

from __future__ import annotations

import base64
import binascii
import re

from .constants import CODE_LENGTH
from .errors import ParseError

__all__ = [
    "RECORD_TYPES",
    "code_from_record_url",
    "decode_vestiging_slug",
    "encode_vestiging_slug",
    "record_type_from_url",
]

RECORD_TYPES = ("zorgverlener", "onderneming", "vestiging")

_CODE_RE = re.compile(rf"^[0-9]{{{CODE_LENGTH}}}$")
_HEX_RE = re.compile(r"^(?:[0-9a-fA-F]{2})+$")
# Detail URLs are always ``…/agb-register/<type>-<code or slug>``.
_URL_RE = re.compile(
    r"/agb-register/(zorgverlener|onderneming|vestiging)-([^/?#]+)",
    re.IGNORECASE,
)


def decode_vestiging_slug(slug: str) -> str:
    """Decode a vestiging URL slug to its eight-digit AGB-code.

    Strictly hex -> ASCII base64 -> eight ASCII digits. Anything else, including
    a plain eight-digit code, raises `ParseError`; use `code_from_record_url`
    when either form may turn up.
    """
    raw = (slug or "").strip()
    if not raw or not _HEX_RE.match(raw):
        raise ParseError(f"vestiging slug is not even-length hex: {slug!r}")
    try:
        b64 = binascii.unhexlify(raw).decode("ascii")
        code = base64.b64decode(b64, validate=True).decode("ascii")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ParseError(f"vestiging slug is not hex(base64(code)): {slug!r}") from exc
    if not _CODE_RE.match(code):
        raise ParseError(f"vestiging slug does not decode to {CODE_LENGTH} digits: {slug!r}")
    return code


def encode_vestiging_slug(agbcode: str) -> str:
    """Encode an eight-digit AGB-code as the lower-case hex(base64) URL slug."""
    code = (agbcode or "").strip()
    if not _CODE_RE.match(code):
        raise ParseError(f"not an {CODE_LENGTH}-digit AGB-code: {agbcode!r}")
    return binascii.hexlify(base64.b64encode(code.encode("ascii"))).decode("ascii")


def record_type_from_url(url: str) -> str | None:
    """Return the record type a detail URL addresses, or None when unrecognized."""
    match = _URL_RE.search(url or "")
    if not match:
        return None
    return match.group(1).lower()


def code_from_record_url(url: str) -> str:
    """Return the eight-digit code a detail URL addresses, or "" when unreadable.

    Accepts both vestiging URL forms: a plain eight-digit tail is used as is, any
    other tail is decoded as a slug. Raises nothing; the caller decides whether a
    missing code is fatal.
    """
    match = _URL_RE.search(url or "")
    if not match:
        return ""
    tail = match.group(2)
    if _CODE_RE.match(tail):
        return tail
    try:
        return decode_vestiging_slug(tail)
    except ParseError:
        return ""

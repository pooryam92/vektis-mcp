"""Pure HTML parsing for the public AGB-register. No I/O, no network, no clock.

The source markup below was observed on 2026-09-12 and is recorded,
with the surprises, in `docs/agb-source-notes.md`. Read that file first when the
site's layout changes; the map from it to this module is:

Search results (`POST zoeken/resultaten` -> redirect GET)
  page wrapper        `section.js-search-results` (`data-value` echoes zorgpartijtype)
  results wrapper     `div#resultsContainer`
  count heading       `h2` containing "<N> Zoekresultaten" (no thousands separator
                      today; separators are tolerated anyway)
  render cap marker   `div#500PlusAlert` ("Er zijn meer dan 500 resultaten: …")
  empty page          "zoekopdracht leverde geen resultaten op" (inside an
                      `div.alert-info`; no heading, no table)
  rows                `table.js-datatable > tbody.card-table__body > tr`, seven
                      direct `td`s: [0] type in `data-search` (the cell text is
                      only an `<svg>`), [1] name + detail `a[href]`, [2] AGB-code
                      or the literal `-` for vestigingen, [3] adres, [4] postcode,
                      [5] plaats, [6] einddatum "YYYY-MM-DD" or `-`.
                      `tfoot.card-table__foot` holds DataTables filter inputs, so
                      never key on `tr`/`td` globally.

Detail pages (`GET /agb-register/<type>-<code|slug>`)
  not found           HTTP 404 plus `h1` "De pagina die je zoekt is niet gevonden"
                      (a wrong-type URL for an existing code answers the same)
  record type         `li.breadcrumb-item.active` ("Zorgverlener" /
                      "Onderneming <naam>" / "Vestiging <naam>") and the bare text
                      node after `h1` in `header … > div.col`.
                      The right-hand header column is labelled "Organisatie" on
                      all three types — never use it.
  naam                `header h1.h2`, empty on zorgverlener pages -> take
                      Basisregistratie "Naam"
  AGB-code            `header div.h4.mb-0`, `-` on vestiging pages (a vestiging
                      page never shows its code; see parse_record_identity)
  start / einde       `header` spans "Start: DD-MM-YYYY" / "Einde: -"
  ended marker        `div.alert.warning` … "Deze AGB-code is beëindigd"
                      (class is `alert warning`, not `alert-warning`)
  basisregistratie    `section` whose `h2` is "Basisregistratie", `dl > dt + dd`
  contact             `h2` "Contactgegevens"; sub-blocks keyed by `div.mb-2.h5`
                      labels "Adresgegevens" / "E-mail" / "Telefoonnummer";
                      addresses are `<br>`-separated lines in `p.mb-0`
  kwalificaties       `h3.h4` "Mijn kwalificaties" -> next `div.row` of
                      `div.card > div.card-body`; title "<naam> <4-digit code>"
  erkenningen         `h3.h4` "Mijn erkenningen" -> same shape; the `dt` that is
                      neither Start nor Einde carries the number (KvK-/BIG-nummer)
  relaties            one `table` per relation, scoped by its exact
                      `caption.visually-hidden` (see `_RELATION_TABLES`); the
                      mobile-only `.d-md-none` `card-table-card` duplicate of a
                      relation is ignored.

Conventions: all dates are normalized to ISO `YYYY-MM-DD` ("-" becomes ""), all
text is whitespace-collapsed with nbsp stripped, and anything recognized but
structurally wrong raises `ParseError` rather than being silently dropped.

Hardening: table columns are mapped by required, unique header labels, with a
single direct tbody; the positions and styling classes above are observations,
not requirements. Captions need no styling class. Heading levels are flexible
for section discovery, contact labels do not depend on styling, and identity
codes can be read from standalone header text. Missing requested sections fail
except for the known absence of contact on individual providers. This deliberately
prefers an explicit failure for unseen valid layouts over silent partial data.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Literal
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from ..models import (
    Adres,
    Basisregistratie,
    Contact,
    Erkenning,
    Kwalificatie,
    Record,
    RecordSection,
    RecordType,
    Relatie,
    Relaties,
    SearchResult,
    Status,
)
from .constants import BASE_URL, CODE_LENGTH
from .errors import ParseError
from .slugs import (
    code_from_record_url,
    decode_vestiging_slug,
    encode_vestiging_slug,
    record_type_from_url,
)
from .types import ParsedSearchPage, RecordIdentity, SourcePage

__all__ = [
    "DEFAULT_SECTIONS",
    "classify_record_page",
    "decode_vestiging_slug",
    "encode_vestiging_slug",
    "parse_record",
    "parse_record_identity",
    "parse_search",
]

# docs/tools.md: omitting `sections` requests these two. Exposed here so the service and
# the tool adapters share one definition of the default.
DEFAULT_SECTIONS: tuple[RecordSection, ...] = ("basisregistratie", "kwalificaties")

_ALL_SECTIONS: tuple[RecordSection, ...] = (
    "basisregistratie",
    "contact",
    "kwalificaties",
    "erkenningen",
    "relaties",
)

_RECORD_TYPES: tuple[RecordType, ...] = ("zorgverlener", "onderneming", "vestiging")

_CODE_RE = re.compile(rf"^[0-9]{{{CODE_LENGTH}}}$")
_COUNT_RE = re.compile(r"([0-9][0-9.,]*)\s*Zoekresultaten")
_EMPTY_MARKER = "zoekopdracht leverde geen resultaten op"
_CAP_MARKER_ID = "500PlusAlert"
_NOT_FOUND_MARKER = "de pagina die je zoekt is niet gevonden"
_ENDED_MARKER = "deze agb-code is beëindigd"
_ISO_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_NL_DATE_RE = re.compile(r"^[0-9]{2}-[0-9]{2}-[0-9]{4}$")
# Kwalificatie card titles append the four-digit code: "Huisarts 0101".
_KWALIFICATIE_TITLE_RE = re.compile(r"^(?P<naam>.*\S)\s+(?P<code>[0-9]{4})$")
_PERIOD_LABELS = ("Start", "Einde")


# --- text helpers ----------------------------------------------------------


def _norm(value: str | None) -> str:
    """Collapse whitespace and strip nbsp / zero-width characters."""
    if not value:
        return ""
    cleaned = value.replace("\xa0", " ").replace("​", "")
    return " ".join(cleaned.split())


def _text(node: Node | None) -> str:
    return _norm(node.text()) if node is not None else ""


def _iso_date(value: str, *, what: str) -> str:
    """Normalize a source date to ISO. "-" / "" become ""; junk is a ParseError."""
    raw = _norm(value)
    if raw in ("", "-"):
        return ""
    if _ISO_DATE_RE.match(raw):
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError as exc:
            raise ParseError(f"{what} is not a valid date: {raw!r}") from exc
        return raw
    if _NL_DATE_RE.match(raw):
        try:
            return datetime.strptime(raw, "%d-%m-%Y").strftime("%Y-%m-%d")
        except ValueError as exc:
            raise ParseError(f"{what} is not a valid date: {raw!r}") from exc
    raise ParseError(f"{what} is not a recognized date: {raw!r}")


def _children(node: Node, tag: str) -> list[Node]:
    """Direct children with this tag, so nested tables cannot contribute cells."""
    return [child for child in node.iter(include_text=False) if child.tag == tag]


def _next_element(node: Node) -> Node | None:
    """The next sibling element, skipping text nodes."""
    sibling = node.next
    while sibling is not None and sibling.tag == "-text":
        sibling = sibling.next
    return sibling


def _classes(node: Node) -> set[str]:
    return set((node.attributes.get("class") or "").split())


def _ancestor_with_class(node: Node, css_class: str) -> Node | None:
    current = node.parent
    while current is not None:
        if css_class in _classes(current):
            return current
        current = current.parent
    return None


def _has_ancestor_class(node: Node, css_class: str) -> bool:
    return _ancestor_with_class(node, css_class) is not None


def _br_lines(node: Node) -> list[str]:
    """Split an element's text on `<br>`, the way the address blocks are built."""
    lines: list[str] = []
    buffer: list[str] = []
    for child in node.iter(include_text=True):
        if child.tag == "br":
            lines.append(_norm("".join(buffer)))
            buffer = []
        else:
            buffer.append(child.text() if child.tag == "-text" else child.text() or "")
    lines.append(_norm("".join(buffer)))
    return [line for line in lines if line]


def _find_by_text(tree: HTMLParser | Node, selector: str, *wanted: str) -> Node | None:
    """First element matching `selector` whose normalized text equals one of `wanted`."""
    targets = {w.casefold() for w in wanted}
    for node in tree.css(selector):
        if _text(node).casefold() in targets:
            return node
    return None


def _dl_pairs(scope: Node) -> list[tuple[str, str]]:
    """`dt`/`dd` pairs of every `dl` inside this scope, in document order."""
    pairs: list[tuple[str, str]] = []
    for definition_list in scope.css("dl"):
        terms = definition_list.css("dt")
        values = definition_list.css("dd")
        if len(terms) != len(values):
            raise ParseError("definition list has mismatched dt/dd counts")
        for term, value in zip(terms, values, strict=True):
            pairs.append((_text(term).rstrip(":"), _text(value)))
    return pairs


# Column names come from table headings, never their visual position.
_SEARCH_COLUMNS = ("type", "naam", "agbcode", "adres", "postcode", "plaats", "einddatum")
_COLUMN_LABELS = {
    "type": "type", "naam": "naam", "naam zorgverlener": "naam",
    "naam onderneming / vestiging": "naam", "agb-code": "agbcode",
    "adres": "adres", "postcode": "postcode", "plaats": "plaats",
    "einddatum": "einddatum", "start": "start", "einde": "einde",
    "rol": "rol", "zorgaanbod": "rol",
}


def _table_columns(table: Node, required: tuple[str, ...]) -> tuple[str, ...]:
    heads = _children(table, "thead")
    rows = _children(heads[0], "tr") if len(heads) == 1 else []
    if len(rows) != 1:
        raise ParseError("table must have one header row")
    columns = []
    for cell in _children(rows[0], "th"):
        label = _text(cell).casefold()
        key = _COLUMN_LABELS.get(label)
        if label == "type" and "rol" in required:
            key = "rol"
        if key is None or key not in required or key in columns:
            raise ParseError(f"unrecognized or duplicate table heading: {label!r}")
        if cell.attributes.get("colspan", "1") != "1" or cell.attributes.get("rowspan", "1") != "1":
            raise ParseError("spanning table headings are unsupported")
        columns.append(key)
    if set(columns) != set(required):
        raise ParseError("table is missing required headings")
    return tuple(columns)


def _table_body(table: Node) -> Node:
    bodies = _children(table, "tbody")
    if len(bodies) != 1:
        raise ParseError("table must have one tbody")
    return bodies[0]


# --- search results --------------------------------------------------------


def parse_search(page: SourcePage) -> ParsedSearchPage:
    """Parse a rendered search-results page.

    Recognizes exactly three shapes: a results table, the "no results" page, and
    the explicit render-cap marker. Everything else — the bare search form, a 419
    "Page Expired" body, unrelated HTML — is a `ParseError`: an unrecognized page
    must never read as zero results. No deduplication, filtering or limiting
    happens here; that is the service's job.
    """
    tree = HTMLParser(page.html)
    scope = tree.css_first("div#resultsContainer") or tree.css_first("section.js-search-results")
    search_scope = scope if scope is not None else tree

    table = _results_table(search_scope)
    cap_marker = tree.css_first(f"#{_CAP_MARKER_ID}") is not None
    is_empty = _EMPTY_MARKER in _norm(search_scope.text()).casefold()

    if table is None and not is_empty and not cap_marker:
        raise ParseError("not a recognized AGB search-results page")
    if cap_marker and table is None:
        # The marker says the source rendered 500 rows; a page showing it with
        # no table at all was misread, and must not pass as a truncated set.
        raise ParseError("search page shows the render-cap marker but no results table")

    rows: list[SearchResult] = []
    if table is not None:
        columns = _table_columns(table, _SEARCH_COLUMNS)
        body = _table_body(table)
        rows = [_parse_search_row(row, columns) for row in _children(body, "tr")]

    if is_empty and rows:
        raise ParseError("search page shows the empty marker and result rows at once")

    return ParsedSearchPage(
        rows=rows,
        source_total=_parse_count(search_scope),
        raw_row_count=len(rows),
        is_empty=is_empty,
        cap_marker=cap_marker,
    )


def _results_table(scope: HTMLParser | Node) -> Node | None:
    """The results table. `js-datatable` is unique to it; relation tables lack it."""
    table = scope.css_first("table.js-datatable")
    if table is not None:
        return table
    for candidate in scope.css("table"):
        if _text(candidate.css_first("caption")) == "Resultaten":
            return candidate
    return None


def _parse_count(scope: HTMLParser | Node) -> int | None:
    """The "<N> Zoekresultaten" heading count, or None when genuinely absent."""
    for heading in scope.css("h2"):
        match = _COUNT_RE.search(_text(heading))
        if match:
            return int(re.sub(r"[.,]", "", match.group(1)))
    match = _COUNT_RE.search(_norm(scope.text()))
    if match:
        return int(re.sub(r"[.,]", "", match.group(1)))
    return None


def _parse_search_row(row: Node, columns: tuple[str, ...]) -> SearchResult:
    cells = _children(row, "td")
    if len(cells) != len(columns):
        raise ParseError(f"search row has {len(cells)} cells, expected {len(columns)}")

    values = dict(zip(columns, cells, strict=True))
    link = values["naam"].css_first("a[href]")
    href = (link.attributes.get("href") or "").strip() if link is not None else ""
    if not href:
        raise ParseError("search row has no detail link")
    source_url = urljoin(f"{BASE_URL}/", href)

    record_type = _row_record_type(values["type"], source_url)

    agbcode = _row_code(_text(values["agbcode"]), source_url, what="search row")

    return SearchResult(
        record_type=record_type,
        naam=_text(values["naam"]),
        agbcode=agbcode,
        adres=_text(values["adres"]),
        postcode=_text(values["postcode"]),
        plaats=_text(values["plaats"]),
        einddatum=_iso_date(_text(values["einddatum"]), what="search row einddatum"),
        source_url=source_url,
    )


def _row_code(code_text: str, source_url: str, *, what: str) -> str:
    """The row's AGB-code: the code column, else the code in its detail URL.

    Vestiging rows and vestiging relations show `-`, so the code exists only in
    the URL. A shown code must be exactly eight digits and must agree with the
    URL; nothing is ever padded, truncated or guessed.
    """
    url_code = code_from_record_url(source_url)
    if code_text in ("", "-"):
        agbcode = url_code
    else:
        if not _CODE_RE.match(code_text):
            raise ParseError(f"{what} AGB-code is not {CODE_LENGTH} digits: {code_text!r}")
        if url_code and url_code != code_text:
            raise ParseError(f"{what} AGB-code {code_text!r} contradicts its URL {url_code!r}")
        agbcode = code_text
    if not _CODE_RE.match(agbcode):
        raise ParseError(f"{what} has no {CODE_LENGTH}-digit AGB-code (cell {code_text!r})")
    return agbcode


def _row_record_type(type_cell: Node, source_url: str) -> RecordType:
    """Type from `data-search` (the cell text is only an icon), cross-checked
    against the detail URL prefix. A disagreement is a parse failure."""
    label = _norm(type_cell.attributes.get("data-search")).casefold() or None
    from_label = label if label in _RECORD_TYPES else None
    from_url = record_type_from_url(source_url)
    if from_label and from_url and from_label != from_url:
        raise ParseError(f"search row type {from_label!r} contradicts its URL {from_url!r}")
    record_type = from_label or from_url
    if record_type is None:
        raise ParseError("search row has no recognizable record type")
    return record_type  # type: ignore[return-value]


# --- detail pages: classification and identity -----------------------------


def classify_record_page(page: SourcePage) -> Literal["record", "not_found"]:
    """Decide whether a fetched detail URL is a record or a recognized absence.

    A plain 404 (nonexistent code *and* wrong type for an existing code) or the
    rendered not-found page is absence. A recognized detail layout is a record.
    Anything else is a `ParseError`, so an outage or layout change can never be
    reported as "this code does not exist".
    """
    tree = HTMLParser(page.html)
    if page.status_code == 404:
        return "not_found"
    if _NOT_FOUND_MARKER in _norm(tree.text()).casefold():
        return "not_found"
    if _looks_like_record(tree):
        return "record"
    raise ParseError("not a recognized AGB detail page")


def _identity_header(tree: HTMLParser) -> Node | None:
    header = tree.css_first("section.accent-block header")
    if header is not None:
        return header
    for candidate in tree.css("header"):
        if candidate.css_first("h1") is not None:
            return candidate
    return None


def _breadcrumb(tree: HTMLParser) -> tuple[RecordType | None, str]:
    """(type, trailing name) from `li.breadcrumb-item.active`."""
    crumb = _text(tree.css_first("li.breadcrumb-item.active"))
    if not crumb:
        return None, ""
    head, _, tail = crumb.partition(" ")
    candidate = head.casefold()
    if candidate in _RECORD_TYPES:
        return candidate, _norm(tail)  # type: ignore[return-value]
    return None, ""


def _header_type(header: Node) -> RecordType | None:
    """The bare text node after `h1` inside `header … > div.col`."""
    heading = header.css_first("h1")
    if heading is None:
        return None
    column = heading.parent
    if column is None:
        return None
    remainder = _text(column)
    name = _text(heading)
    if name and name in remainder:
        remainder = _norm(remainder.replace(name, "", 1))
    candidate = remainder.partition(" ")[0].casefold()
    return candidate if candidate in _RECORD_TYPES else None  # type: ignore[return-value]


def _header_code(header: Node) -> str:
    codes = {
        _text(node) for node in header.css("*")
        if not list(node.iter(include_text=False)) and _CODE_RE.fullmatch(_text(node))
    }
    if len(codes) > 1:
        raise ParseError("identity header contains conflicting AGB-codes")
    if codes:
        return codes.pop()
    return _text(header.css_first("div.h4.mb-0"))


def _looks_like_record(tree: HTMLParser) -> bool:
    header = _identity_header(tree)
    if header is None:
        return False
    breadcrumb_type, _ = _breadcrumb(tree)
    if breadcrumb_type is None and _header_type(header) is None:
        return False
    has_code = bool(_header_code(header))
    has_period = "Start:" in _norm(header.text())
    return has_code or has_period


def parse_record_identity(
    page: SourcePage,
    expected_type: RecordType,
    expected_code: str,
) -> RecordIdentity:
    """Verify a detail page is the requested record and read its identity.

    Type comes from the structural evidence the source notes call reliable: the
    active breadcrumb item and the bare type text node in the header. The
    header's right-hand "Organisatie" label is a template artefact present on all
    three types and is never used.

    Code: zorgverlener and onderneming pages display their eight digits in
    `header div.h4.mb-0`, and it must equal `expected_code`. A **vestiging page
    never shows its own code** (the header shows `-`); the only occurrence is the
    requested URL echoed in `meta[itemprop=url]` / `meta[property=og:url]`. So for
    a vestiging the code is taken from that echo — or from `page.url` — and
    checked against `expected_code`; when neither carries a readable code,
    `expected_code` is trusted, since the probe URL was built from it.

    Name: a zorgverlener page's `h1` is empty, so the name comes from
    Basisregistratie "Naam"; onderneming and vestiging use the `h1`, falling back
    to the breadcrumb tail and Basisregistratie.

    Any mismatch, or an unrecognized layout, raises `ParseError`.
    """
    if expected_type not in _RECORD_TYPES:
        raise ParseError(f"unknown expected record type {expected_type!r}")
    if not _CODE_RE.match(_norm(expected_code)):
        raise ParseError(f"expected code is not {CODE_LENGTH} digits: {expected_code!r}")
    expected_code = _norm(expected_code)

    tree = HTMLParser(page.html)
    header = _identity_header(tree)
    if header is None or not _looks_like_record(tree):
        raise ParseError("not a recognized AGB detail page")

    breadcrumb_type, breadcrumb_name = _breadcrumb(tree)
    header_type = _header_type(header)
    if breadcrumb_type and header_type and breadcrumb_type != header_type:
        raise ParseError(
            f"breadcrumb says {breadcrumb_type!r} but the header says {header_type!r}"
        )
    record_type = breadcrumb_type or header_type
    if record_type is None:
        raise ParseError("detail page shows no record type")
    if record_type != expected_type:
        raise ParseError(f"page is a {record_type!r}, expected {expected_type!r}")

    shown_code = _header_code(header)
    if record_type == "vestiging":
        if shown_code not in ("", "-") and shown_code != expected_code:
            raise ParseError(
                f"vestiging page shows code {shown_code!r}, expected {expected_code!r}"
            )
        echoed = _echoed_code(tree, page.url)
        if echoed and echoed != expected_code:
            raise ParseError(
                f"vestiging page echoes code {echoed!r}, expected {expected_code!r}"
            )
        agbcode = expected_code
    else:
        if not _CODE_RE.match(shown_code):
            raise ParseError(f"detail page shows no {CODE_LENGTH}-digit code ({shown_code!r})")
        if shown_code != expected_code:
            raise ParseError(f"page shows code {shown_code!r}, expected {expected_code!r}")
        agbcode = shown_code

    naam = _record_name(tree, header, record_type, breadcrumb_name)
    if not naam:
        raise ParseError("detail page shows no name")
    return RecordIdentity(record_type=record_type, agbcode=agbcode, naam=naam)


def _echoed_code(tree: HTMLParser, page_url: str) -> str:
    """The code from the URL the page echoes, else from the URL actually fetched."""
    for selector in ('meta[property="og:url"]', 'meta[itemprop="url"]'):
        meta = tree.css_first(selector)
        if meta is None:
            continue
        code = code_from_record_url(meta.attributes.get("content") or "")
        if code:
            return code
    return code_from_record_url(page_url)


def _record_name(
    tree: HTMLParser,
    header: Node,
    record_type: RecordType,
    breadcrumb_name: str,
) -> str:
    basis_naam = ""
    section = _basisregistratie_section(tree)
    if section is not None:
        for label, value in _dl_pairs(section):
            if label == "Naam":
                basis_naam = value
                break
    if record_type == "zorgverlener":
        # The h1 is always empty for individuals.
        return basis_naam or _text(header.css_first("h1"))
    return _text(header.css_first("h1")) or breadcrumb_name or basis_naam


# --- detail pages: sections ------------------------------------------------


def parse_record(
    page: SourcePage,
    identity: RecordIdentity,
    sections: Sequence[RecordSection],
) -> Record:
    """Build a `Record` from a verified detail page.

    `sections` is deduplicated while preserving order. A requested section the
    page does not provide for this type (contact on a zorgverlener) is reported in
    `unavailable_sections` with its field left None; a provided section with no
    entries yields an empty list or an empty model, never "unavailable". Sections
    that were not requested stay None. Each section is parsed with selectors
    scoped to its own container, so nested tables cannot leak into one another; a
    section that is present but structurally wrong raises `ParseError`.
    """
    tree = HTMLParser(page.html)
    requested = _dedupe_sections(sections)

    containers = {
        "basisregistratie": _basisregistratie_section(tree),
        "contact": _contact_container(tree),
        "kwalificaties": _cards_container(tree, "Mijn kwalificaties") if "kwalificaties" in requested else None,
        "erkenningen": _cards_container(tree, "Mijn erkenningen") if "erkenningen" in requested else None,
    }
    relation_tables = _relation_tables(tree) if "relaties" in requested else []
    available = {
        "basisregistratie": containers["basisregistratie"] is not None,
        "contact": containers["contact"] is not None,
        "kwalificaties": containers["kwalificaties"] is not None,
        "erkenningen": containers["erkenningen"] is not None,
        "relaties": bool(relation_tables) or _relaties_heading(tree),
    }

    unavailable = [section for section in requested if not available[section]]
    unexpected = [section for section in unavailable
                  if not (section == "contact" and identity.record_type == "zorgverlener")]
    if unexpected:
        raise ParseError(f"requested sections missing or unrecognized: {', '.join(unexpected)}")
    parsed: dict[str, object] = {}
    for section in requested:
        if not available[section]:
            continue
        if section == "basisregistratie":
            parsed[section] = _parse_basisregistratie(containers["basisregistratie"])
        elif section == "contact":
            parsed[section] = _parse_contact(containers["contact"])
        elif section == "kwalificaties":
            parsed[section] = _parse_kwalificaties(containers["kwalificaties"])
        elif section == "erkenningen":
            parsed[section] = _parse_erkenningen(containers["erkenningen"])
        elif section == "relaties":
            parsed[section] = _parse_relaties(relation_tables)

    return Record(
        record_type=identity.record_type,
        agbcode=identity.agbcode,
        source_url=page.url,
        retrieved_at=page.fetched_at,
        status=_parse_status(tree),
        naam=identity.naam,
        requested_sections=list(requested),
        unavailable_sections=unavailable,
        basisregistratie=parsed.get("basisregistratie"),  # type: ignore[arg-type]
        contact=parsed.get("contact"),  # type: ignore[arg-type]
        kwalificaties=parsed.get("kwalificaties"),  # type: ignore[arg-type]
        erkenningen=parsed.get("erkenningen"),  # type: ignore[arg-type]
        relaties=parsed.get("relaties"),  # type: ignore[arg-type]
    )


def _dedupe_sections(sections: Iterable[RecordSection]) -> list[RecordSection]:
    seen: list[RecordSection] = []
    for section in sections:
        if section not in _ALL_SECTIONS:
            raise ParseError(f"unknown record section {section!r}")
        if section not in seen:
            seen.append(section)
    return seen


def _parse_status(tree: HTMLParser) -> Status:
    """Ended flag from the explicit text marker, dates from header spans."""
    beeindigd = _ENDED_MARKER in _norm(tree.text()).casefold()

    start = einde = ""
    header = _identity_header(tree)
    if header is not None:
        for span in header.css("span"):
            label, sep, value = _text(span).partition(":")
            if not sep:
                continue
            if label == "Start":
                start = _iso_date(value, what="header start date")
            elif label == "Einde":
                einde = _iso_date(value, what="header end date")
    return Status(beeindigd=beeindigd, start=start, einde=einde)


# Basisregistratie

def _basisregistratie_section(tree: HTMLParser) -> Node | None:
    heading = _find_by_text(tree, "h1, h2, h3, h4, h5, h6", "Basisregistratie")
    if heading is None:
        return None
    # Live markup wraps the fields in a <section> of their own, which keeps the
    # `dl` scraping away from the Contactgegevens block in the same container.
    return heading.parent


def _parse_basisregistratie(section: Node | None) -> Basisregistratie:
    if section is None:  # pragma: no cover - guarded by availability
        raise ParseError("Basisregistratie section missing")
    fields: dict[str, str] = {}
    handelsnamen: list[str] = []
    for label, value in _dl_pairs(section):
        if not label:
            raise ParseError("Basisregistratie field has an empty label")
        if label.startswith("Handelsnaam"):
            if value:
                handelsnamen.append(value)
        else:
            fields.setdefault(label, value)
    if "Naam" not in fields:
        raise ParseError("Basisregistratie has no 'Naam' field")
    return Basisregistratie(
        naam=fields["Naam"],
        geboortenaam=fields.get("Geboortenaam", ""),
        geslacht=fields.get("Geslacht", ""),
        academische_titel=fields.get("Academische titel", ""),
        handelsnamen=handelsnamen,
    )


# Contactgegevens

def _contact_container(tree: HTMLParser) -> Node | None:
    """The `div.container` that holds the "Contactgegevens" heading."""
    heading = _find_by_text(tree, "h1, h2, h3, h4, h5, h6", "Contactgegevens")
    if heading is None:
        return None
    return _ancestor_with_class(heading, "container") or heading.parent


def _contact_block(container: Node, label: str) -> Node | None:
    """Find a contact block by its label, independent of styling classes."""
    for marker in container.css("div, h3, h4, h5, h6"):
        if not list(marker.iter(include_text=False)) and _text(marker).casefold() == label.casefold():
            return marker.parent
    return None


def _parse_contact(container: Node | None) -> Contact:
    if container is None:  # pragma: no cover - guarded by availability
        raise ParseError("Contactgegevens section missing")

    adressen: list[Adres] = []
    address_block = _contact_block(container, "Adresgegevens")
    if address_block is not None:
        for heading in address_block.css("h3"):
            soort = _text(heading)
            if not soort:
                raise ParseError("address block has an unlabelled address")
            holder = heading.parent
            paragraph = holder.css_first("p") if holder is not None else None
            if paragraph is None:
                raise ParseError(f"address {soort!r} has no address lines")
            lines = _br_lines(paragraph)
            straat = lines[0] if lines else ""
            postcode, plaats = _split_pair(lines[1]) if len(lines) > 1 else ("", "")
            provincie, land = _split_pair(lines[2]) if len(lines) > 2 else ("", "")
            adressen.append(
                Adres(
                    soort=soort,
                    straat=straat,
                    postcode=postcode,
                    plaats=plaats,
                    provincie=provincie,
                    land=land,
                )
            )

    if not any(_contact_block(container, label) is not None
               for label in ("Adresgegevens", "E-mail", "Telefoonnummer")):
        raise ParseError("Contactgegevens has no recognized contact blocks")
    if address_block is not None and not adressen and _text(address_block) != "Adresgegevens":
        raise ParseError("address block contains unrecognized addresses")

    # models.Contact carries a single e-mail and a single phone number; the source
    # groups them by soort ("Algemeen"), so the first entry of each is kept.
    return Contact(
        adressen=adressen,
        email=_first_contact_value(container, "E-mail"),
        telefoon=_first_contact_value(container, "Telefoonnummer"),
    )


def _split_pair(line: str) -> tuple[str, str]:
    head, _, tail = line.partition(",")
    return _norm(head), _norm(tail)


def _first_contact_value(container: Node, label: str) -> str:
    block = _contact_block(container, label)
    if block is None:
        return ""
    heading = block.css_first("h4")
    value = _next_element(heading) if heading is not None else None
    if value is None:
        raise ParseError(f"contact block {label!r} has no value")
    return _text(value)


# Bevoegdheden: kwalificaties and erkenningen

def _cards_container(tree: HTMLParser, heading_text: str) -> Node | None:
    """The `div.row` of cards that follows a "Mijn …" sub-heading."""
    heading = _find_by_text(tree, "h1, h2, h3, h4, h5, h6", heading_text)
    if heading is None:
        return None
    anchor = heading.parent if heading.parent is not None else heading
    container = _next_element(anchor)
    if container is None or "row" not in _classes(container):
        raise ParseError(f"{heading_text!r} is not followed by a card row")
    return container


def _card_bodies(container: Node) -> list[Node]:
    cards = container.css("div.card > div.card-body")
    if not cards and (list(container.iter(include_text=False)) or _text(container)):
        raise ParseError("nonempty card row has no recognizable cards")
    for child in container.iter(include_text=False):
        if _text(child) and not child.css("div.card > div.card-body"):
            raise ParseError("card row contains unrecognized content")
    if len(container.css("div.card")) != len(cards):
        raise ParseError("card row contains unrecognized cards")
    return cards


def _card_fields(card: Node) -> tuple[str, str, str]:
    """(start, einde, nummer) for one Bevoegdheden card."""
    start = einde = nummer = ""
    for label, value in _dl_pairs(card):
        if not label:
            raise ParseError("Bevoegdheden card has an unlabelled field")
        if label == "Start":
            start = _iso_date(value, what="card start date")
        elif label == "Einde":
            einde = _iso_date(value, what="card end date")
        else:
            # The label is the number kind: KvK-nummer, BIG-nummer, …
            nummer = nummer or value
    return start, einde, nummer


def _parse_kwalificaties(container: Node | None) -> list[Kwalificatie]:
    if container is None:  # pragma: no cover - guarded by availability
        raise ParseError("Mijn kwalificaties missing")
    result: list[Kwalificatie] = []
    for card in _card_bodies(container):
        title = _text(card.css_first("h3"))
        match = _KWALIFICATIE_TITLE_RE.match(title)
        if not match:
            raise ParseError(f"kwalificatie title carries no four-digit code: {title!r}")
        start, einde, _ = _card_fields(card)
        result.append(
            Kwalificatie(
                code=match.group("code"),
                naam=match.group("naam"),
                start=start,
                einde=einde,
            )
        )
    return result


def _parse_erkenningen(container: Node | None) -> list[Erkenning]:
    if container is None:  # pragma: no cover - guarded by availability
        raise ParseError("Mijn erkenningen missing")
    result: list[Erkenning] = []
    for card in _card_bodies(container):
        naam = _text(card.css_first("h3"))
        if not naam:
            raise ParseError("erkenning card has no title")
        start, einde, nummer = _card_fields(card)
        result.append(Erkenning(naam=naam, nummer=nummer, start=start, einde=einde))
    return result


# Relaties

# caption.visually-hidden -> (field on models.Relaties, column layout)
_RELATION_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "Ik ben werkzaam bij de volgende vestigingen": (
        "vestigingen",
        ("naam", "rol", "agbcode", "start", "einde"),
    ),
    "Ik heb een arbeidsrelatie met": (
        "arbeidsrelaties",
        ("naam", "rol", "agbcode", "start", "einde"),
    ),
    "Deze onderneming heeft de volgende vestigingen": (
        "vestigingen",
        ("naam", "adres", "agbcode", "start", "einde"),
    ),
    "Bij deze onderneming werken de volgende zorgverleners": (
        "zorgverleners",
        ("naam", "rol", "agbcode", "start", "einde"),
    ),
    "Deze onderneming heeft een relatie met de volgende ondernemingen": (
        "ondernemingen",
        ("naam", "rol", "agbcode", "start", "einde"),
    ),
    "Is vestiging van onderneming": (
        "onderneming",
        ("naam", "agbcode", "start", "einde"),
    ),
    "Werkzaam als zorgverlener": (
        "zorgverleners",
        ("naam", "rol", "agbcode", "start", "einde"),
    ),
}

_RELATION_LISTS = ("vestigingen", "zorgverleners", "ondernemingen", "arbeidsrelaties")


def _relation_tables(tree: HTMLParser) -> list[tuple[str, tuple[str, ...], Node]]:
    """Relation tables scoped by their exact caption, in document order.

    Several relation tables share one page block, so scoping by caption (never
    "all rows in the block") is what keeps them apart. The mobile-only
    `.d-md-none` duplicate of a relation is not a table and is skipped anyway;
    the explicit ancestor check keeps that true if the markup changes.
    """
    tables: list[tuple[str, tuple[str, ...], Node]] = []
    for caption in tree.css("caption"):
        spec = _RELATION_TABLES.get(_text(caption))
        if spec is None:
            if not _has_ancestor_class(caption, "d-md-none"):
                raise ParseError(f"unrecognized detail table caption: {_text(caption)!r}")
            continue
        table = caption.parent
        if table is None or table.tag != "table":
            raise ParseError("relation caption is not inside a table")
        if _has_ancestor_class(table, "d-md-none"):
            continue
        tables.append((spec[0], spec[1], table))
    recognized = {table.mem_id for _, _, table in tables}
    for table in tree.css("table"):
        if not _has_ancestor_class(table, "d-md-none") and table.mem_id not in recognized:
            raise ParseError("unrecognized detail table; relations may be incomplete")
    return tables


def _relaties_heading(tree: HTMLParser) -> bool:
    """True when the page shows a relations heading even with no tables."""
    if _find_by_text(tree, "h1, h2, h3, h4, h5, h6", "Relaties", *(_RELATION_TABLES.keys())) is not None:
        return True
    for heading in tree.css("h2"):
        if _text(heading).split(" ")[0] in ("Relaties", "Vestigingen", "Zorgverleners", "Ondernemingen"):
            return True
    for title in tree.css("span.card-table-header__title"):
        if _text(title) in _RELATION_TABLES:
            return True
    return False


def _parse_relaties(tables: list[tuple[str, tuple[str, ...], Node]]) -> Relaties:
    relaties = Relaties()
    for field, columns, table in tables:
        columns = _table_columns(table, columns)
        body = _table_body(table)
        rows = [_parse_relation_row(row, columns) for row in _children(body, "tr")]
        if field == "onderneming":
            if rows and relaties.onderneming is None:
                relaties.onderneming = rows[0]
        else:
            getattr(relaties, field).extend(rows)
    return relaties


def _parse_relation_row(row: Node, columns: tuple[str, ...]) -> Relatie:
    cells = _children(row, "td")
    if len(cells) != len(columns):
        raise ParseError(
            f"relation row has {len(cells)} cells, expected {len(columns)}"
        )
    values = dict(zip(columns, cells, strict=True))

    name_cell = values["naam"]
    link = name_cell.css_first("a[href]")
    href = (link.attributes.get("href") or "").strip() if link is not None else ""
    naam = _text(name_cell)
    if not naam:
        raise ParseError("relation row has no name")

    # `-` whenever the related record is a vestiging; decode it from the link.
    target_url = urljoin(f"{BASE_URL}/", href) if href else ""
    agbcode = _row_code(_text(values["agbcode"]), target_url, what="relation row")

    return Relatie(
        naam=naam,
        agbcode=agbcode,
        rol=_text(values["rol"]) if "rol" in values else "",
        adres=_text(values["adres"]) if "adres" in values else "",
        start=_iso_date(_text(values["start"]), what="relation start date"),
        einde=_iso_date(_text(values["einde"]), what="relation end date"),
    )

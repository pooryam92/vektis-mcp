# AGB source notes (live verification, 2026-09-12)

What the public register at `https://www.vektis.nl/agb-register/` actually does
today, as verified before implementation; [design.md](design.md) describes what
was built on it.
Everything below was observed live on **2026-09-12** (Europe/Amsterdam, afternoon)
unless a line says "not verified". Raw responses were kept outside the repository
because they contain personal data; `tests/fixtures/agb/` holds synthetic
reductions of the same structures.

Probe discipline: 27 requests total, single-threaded, ≥ 2.0 s apart, one aborted
client-side before a reply. User-Agent `vektis-mcp/0.1` throughout.

## 0. Access, User-Agent, robots.txt

| Check | Result |
| --- | --- |
| `GET /robots.txt` | `200`, `text/plain`, 560 bytes |
| UA `vektis-mcp/0.1` | Accepted everywhere; no challenge, no 403, no reCAPTCHA enforcement. The Chrome fallback UA was **not needed**. |
| Cookies | `XSRF-TOKEN` (readable) and `vektis_session` (httponly), `Max-Age=7200`, `secure`, `samesite=none`, set on the form GET and re-set by every POST |
| `cache-control` | `no-cache, private` on form/results pages |

`robots.txt`: `User-agent: *` has an empty `Disallow:` (everything allowed). A
second group lists `GPTBot, ChatGPT-User, ClaudeBot, Claude-Web, Anthropic-ai,
Google-Extended, CCBot, PerplexityBot, Bytespider, Diffbot, FacebookBot,
ImagesiftBot, Omgilibot, Omgili` and disallows exactly the paths this server uses:
`/agb-register/zoeken/resultaten`, `/agb-register/zorgverlener-`,
`/agb-register/onderneming-`, `/agb-register/vestiging-`. An MCP server acting for
a user is not one of those crawlers, but the identifiable default UA must not be
changed to impersonate a browser, and it must not be renamed to anything
containing those tokens.

## 1. Search form: `GET /agb-register/zoeken` → `200`

CSRF token: `<input type="hidden" name="_token" value="…" autocomplete="off">`,
inside `form.js-agb-search-form` (40 chars, Laravel). The existing regex
`name="_token"\s+value="([^"]+)"` still matches. There is also a
`<meta name="csrf-token" content="…">` in `<head>` with a *different* value —
use the form input, not the meta.

Form: `action="https://www.vektis.nl/agb-register/zoeken/resultaten#resultaten"`,
`method="POST"`, class `js-agb-search-form`. Plain native submit (no fetch/XHR;
the JS only validates, switches tabs and fills the kwalificaties dropdown), so
the wire format is standard `application/x-www-form-urlencoded`.

All fields, in document order:

| Name | Type | Notes |
| --- | --- | --- |
| `_token` | hidden | CSRF |
| `zorgpartijtype` | radio ×2 | `zorgverlener` (id `frmInputZorgpartijType1`, `checked`) and `onderneming,vestiging` (id `frmInputZorgpartijType2`). Exactly the two values the design assumes. |
| `agbcode` | text | id `frmInputAgbcode`, label "Zoek op AGB-code of naam", `pattern="^\d+$\|^[A-Za-zÀ-ÖØ-öø-ÿ0-9\s\-'\(\).]+$"` |
| `zorgsoort` | select ×2 | **Two selects share the name.** `#frmSelectZorgsoort` inside `fieldset#zorgsoortZorgverlener` (25 options) and `#frmSelectZorgsoortVestiging` inside `fieldset#zorgsoortVestiging` (73 options, rendered `hidden disabled`). The tab JS enables one and disables the other, so a browser sends one `zorgsoort`. Send exactly one value. Empty option value `""` = "00 - Alle zorgsoorten". |
| `kwalificaties[]` | checkbox, repeated | Rendered client-side from `<template id="qualificationsItem">`: `<input id="q_{id}" type="checkbox" name="kwalificaties[]" value="{value}" {checked}>` where `{value}` is the four-digit code. **Encoding is a repeated bracketed key**, e.g. `kwalificaties%5B%5D=0101&kwalificaties%5B%5D=0110`. Not comma-joined. The container `#frmKwalificaties` sits in the shared fieldset (not in a tab), so it applies to both sides. |
| `plaats` | text | id `frmInputPlaats`, in `fieldset#tab2` (organisation tab only), ``pattern="^[A-Za-zÀ-ÖØ-öø-ÿ\s\-'`]+$"`` |
| `postcode` | text | id `frmInputPostcode`, `fieldset#tab2`, `pattern="[1-9][0-9]{3}\s?([a-zA-Z]{2})?"`, `data-transform-input="upper-case-alpha-numeric"` (the browser upper-cases it) |
| `kvknummer` | text | id `frmInputKvknummer`, `fieldset#tab2`, `pattern="^[0-9]{1,12}"` |
| `g-recaptcha-response` | hidden | reCAPTCHA v3 (`action: agbRegisterSearch`). **Not enforced**: every search below succeeded with the field omitted entirely. |

`fieldset#tab2` is `hidden` on the zorgverlener tab and the JS sets `disabled`,
so a browser never sends `plaats`/`postcode`/`kvknummer` for an individual
search. Whether the server would honour them there is **not verified** — keep the
[tool contract](tools.md) (location filters are organisation-only).

Client-side validation (no server equivalent observed): at least one
`[data-required]` field in the visible tab must be non-empty, else the JS shows
"Vul minstens 1 veld in." and blocks submit.

Qualification table (`vektis_mcp/data/codes.json` was extracted from this):
inline `<script>` with `const qualifications = {…};`, keyed by the
`zorgpartijtype` value, i.e. **`"zorgverlener"` and `"onderneming,vestiging"`**
(the repo's JSON renames the second to `organisatie`), then by zorgsoort code:
`{"01": {"code": "01", "description": "Huisartsen", "kwalificaties": {"0101":
{"code": "0101", "description": "Huisarts - 0101", "selected": false}, …}}}`.
Today: 25 zorgsoorten on the zorgverlener side, 73 on the organisation side.
Note the `description` carries the code as a `" - 0101"` suffix, which the
bundled table strips.

## 2. Search: `POST /agb-register/zoeken/resultaten`

Always `302` with `Location: https://www.vektis.nl/agb-register/zoeken/resultaten#resultaten`
(absolute, with fragment) and a fresh `Set-Cookie`; the body is a 498-byte Laravel
redirect stub. The paced follow-up `GET` on that URL returns `200` and the rendered
results. Cookies from the form GET must ride along (the query lives in the session).
`GET /agb-register/zoeken/resultaten` without a prior POST → `302` to
`https://www.vektis.nl/agb-register/zoeken` (confirmed).

Searches run (status of every POST `302`, every redirect GET `200`):

| # | `zorgpartijtype` | fields | heading count | rendered rows |
| --- | --- | --- | --- | --- |
| 1 | `zorgverlener` | `agbcode=0100045`, `zorgsoort=01` | 2 | 2 |
| 2 | `onderneming,vestiging` | `agbcode=`, `zorgsoort=01`, `plaats=Assen`, `postcode=`, `kvknummer=` | 108 | 108 |
| 3 | `onderneming,vestiging` | as #2 plus `postcode=9403` | 14 | 14 |
| 4 | `zorgverlener` | `agbcode=01`, `zorgsoort=01` | 15857 | 500 |
| 5 | `zorgverlener` | as #4 plus `kwalificaties[]=0110` | 381 | 381 |
| 6 | `zorgverlener` | `agbcode=00000000`, `zorgsoort=01` | — (empty page) | 0 |

Conclusions:

- `plaats` filters, and matches as a **case-insensitive substring**, not equality:
  `plaats=Assen` returned ASSEN (46), WASSENAAR (20), SASSENHEIM (16),
  ASSENDELFT (16), VAASSEN (8), ASSENEDE (2, Belgium). Do not promise
  "city equals".
- `postcode` filters and matches as a **prefix**: `postcode=9403` returned only
  `9403xx` rows. Send it upper-case without a space (`1234AB`).
- `kwalificaties[]` filters: 15857 → 381 with one extra value on an otherwise
  identical query. **Open question:** whether two or more values are OR-ed or
  AND-ed was not tested (only a single value was sent live). Verify in the
  implementation smoke check before advertising multi-select semantics.
- `kvknummer` was not exercised live (field name taken from the form).
- An empty `agbcode` is accepted when another criterion is present (#2, #3).
- `agbcode` substring-matching on codes and name matching are unchanged from the
  earlier harvester's findings; searching `01` returns the whole zorgsoort.

### Results page structure

Wrapper: `<section class="js-search-results" data-value="zorgverlener">` —
`data-value` echoes the submitted `zorgpartijtype` (`onderneming,vestiging` on the
organisation side) and is a cheap sanity check. Inside, when there are hits:
`<div class="visually-hidden" id="resultaten"></div>` then `<div id="resultsContainer">`.

Count heading: `<h2 class="h3 mt-4">` containing `"<N> Zoekresultaten"`.
**No thousands separator today** — `15857 Zoekresultaten`, not `15.857`. Keep the
tolerant regex (`([\d.]+)\s*Zoekresultaten`, strip `.`) but do not depend on dots.

Cap marker (explicit, better than the old count-vs-rows heuristic):

```html
<div class="alert-warning alert" id="500PlusAlert" role="alert"><div class="d-flex gap-1"><div class="flex-grow-1">Er zijn meer dan 500 resultaten: niet alle resultaten worden getoond.</div></div></div>
```

Present only on #4 (15857/500); absent on 2, 14, 108, 381 rows. Render cap
confirmed at **500**. So `source_truncated` can key on `#500PlusAlert` (or the
text), with the count > row count as a secondary signal.

Empty page: no `#resultsContainer`, no heading, no table — only

```html
<div class="alert-info alert mt-4" role="alert"><div class="d-flex gap-1"><div class="flex-grow-1">Jouw zoekopdracht leverde geen resultaten op. Pas s.v.p. je zoekopdracht aan en probeer het nog een keer.</div></div></div>
```

inside `section.js-search-results`. The existing marker
`zoekopdracht leverde geen resultaten op` still matches (note the page says
"Jouw zoekopdracht …").

Table: `table.card-table.table.table-striped.js-datatable` >
`caption.visually-hidden` "Resultaten", `thead.card-table__head`,
`tfoot.card-table__foot` (per-column DataTables filter `input`/`select` — a
parser keying on `tr`/`td` globally would pick these up; key on
`tbody.card-table__body > tr` as the old `RowParser` does), `tbody.card-table__body`.

Columns, in order (7), identical on both sides except the name header:

| # | Header | Cell |
| --- | --- | --- |
| 0 | `Type` | `td.card-table__cell.fs-5` with `data-search="Zorgverlener"\|"Onderneming"\|"Vestiging"` and the same value in `data-order`. **Text content is empty** (only an `<svg>` icon: `icon-caregiver` / `icon-locations` / `icon-hospital`). The type must be read from `data-search`. |
| 1 | `Naam zorgverlener` / `Naam onderneming / vestiging` | `td.card-table__cell.card-table__cell--name > a[href]` |
| 2 | `AGB-code` | `a[href]` with the eight digits, **or the literal `-`** for vestiging rows |
| 3 | `Adres` | plain text, empty for zorgverleners |
| 4 | `Postcode` | plain text, no space, often with trailing whitespace (`9403EZ  `) |
| 5 | `Plaats` | `td.…--city`, upper-case |
| 6 | `Einddatum` | `td.…--date` with `data-order` = epoch seconds or `""`; text is `YYYY-MM-DD` or `-` |

Detail URLs in rows (absolute, `https://www.vektis.nl/agb-register/…`):
`zorgverlener-<8 digits>`, `onderneming-<8 digits>`, and for vestigingen only the
encoded form `vestiging-<hex(base64(code))>` (e.g.
`vestiging-4e7a45784d7a55344d44553d`). Slug decoding confirmed unchanged:
`hex → ascii base64 → eight ASCII digits` (`71004592` ⇄ `4e7a45774d4451314f54493d`).
Vestiging rows carry name/address/postcode/plaats but `-` in the code column, so
the code only exists in the URL.

## 3. Detail pages

Fetched with a **fresh cookie-less client per URL, no form GET** — all succeeded,
so sessionless detail access is confirmed.

| URL | Status | Notes |
| --- | --- | --- |
| `/agb-register/zorgverlener-01000404` (active) | `200` | |
| `/agb-register/zorgverlener-01000451` (ended) | `200` | |
| `/agb-register/onderneming-01010001` | `200` | |
| `/agb-register/vestiging-71001001` (plain digits, a 71-code) | `200` | no redirect |
| `/agb-register/vestiging-4e7a45774d4445774d44453d` (same record, encoded) | `200` | no redirect |
| `/agb-register/zorgverlener-99999999` (nonexistent) | `404` | rendered not-found page, no `Location` |
| `/agb-register/onderneming-01000404` (wrong type for an existing zorgverlener code) | `404` | byte-identical to the nonexistent case apart from per-request nonces |

**Vestiging URL forms: no redirect in either direction.** Both render the same
record; the two responses differ only in per-request `nonce`/`csrf-token` values
and in `<meta itemprop="url">` / `<meta property="og:url">`, which echo the
requested URL verbatim. A plain-digit URL works for a `71…` code, so the design's
fourth probe can be dropped: probe `vestiging-<digits>` and treat
`vestiging-<hex(base64)>` only as an input form to normalise, not as a second
candidate. (Verified on one 71-code; the harvest also contains 137 plain-digit
vestiging URLs with 01-codes.)

Not-found is a **plain `404`** with a rendered page: `<h1 class="h3">De pagina die
je zoekt is niet gevonden</h1>`, body text "Sorry, de pagina die je zoekt is niet
gevonden.", a `404-*.svg` image and a site-search form. No redirect to the search
form. A wrong-type URL for an existing code is the same 404 — so a 404 on a probe
is recognised absence *for that URL*, and type disambiguation costs one probe per
candidate type.

### Identity and status (all three types)

The page has no useful `<title>` (always `Vektis`).

| Datum | Where |
| --- | --- |
| Record type | `li.breadcrumb-item.active` text = `Zorgverlener` / `Onderneming <naam>` / `Vestiging <naam>`; and the bare text node after `h1` inside `header .row > .col` = `Zorgverlener` / `Onderneming` / `Vestiging` |
| Name | `header h1.h2` — **empty on zorgverlener pages**, the name for onderneming/vestiging. For a zorgverlener take Basisregistratie → `Naam`. |
| AGB-code | `header .col-md-auto.text-md-end > div.h4.mb-0` — **`-` on vestiging pages**, the eight digits on zorgverlener/onderneming pages. A vestiging page does not display its own code anywhere in the body; the only occurrence is the echoed URL in `meta[itemprop=url]`/`og:url`. Identity for a vestiging must therefore come from the requested URL (decoded slug), not from the page. |
| Start / Einde | two `<span>`s in `header … div.d-flex.gap-4`: `Start: 01-07-1988`, `Einde: -` (or a date). Format `DD-MM-YYYY`, `-` when open. |
| Ended marker | `<div class="alert warning" role="alert">` … `&nbsp;Deze AGB-code is beëindigd`, placed inside `div.read-more.js-read-more` **before** `<header>`. Class is `alert warning` (not `alert-warning`). Absent on active records. |

The right-hand header column is labelled `Organisatie` on **all three** types
(a template artefact) — never use it to determine the type.

### Sections per type

Page body is a sequence of `div.block-pt-lg.block-pb-lg` blocks, each wrapping a
`div.container`. Scope per section; several sections share one block.

| Section | zorgverlener | onderneming | vestiging |
| --- | --- | --- | --- |
| Basisregistratie | yes | yes | yes |
| Contact (`Contactgegevens`) | **absent** | yes | yes |
| Kwalificaties (`Bevoegdheden` → `Mijn kwalificaties`) | yes | yes | yes |
| Erkenningen (`Bevoegdheden` → `Mijn erkenningen`) | yes | yes | yes |
| Relaties | `Relaties`: vestigingen + arbeidsrelaties | `Vestigingen`, `Zorgverleners`, `Ondernemingen` (three `h2`s in one block) | `Is vestiging van onderneming` (in the `Bevoegdheden` block) + `Relaties`: `Werkzaam als zorgverlener` |

**Basisregistratie** — `div.block-pt-lg` > `div.container` > `section` whose
`h2.h3` is `Basisregistratie`; fields are `dl > dt.fw-normal + dd.fw-bold` pairs
spread over `div.col-*` columns. Labels observed: `Naam`, `Geboortenaam`,
`Geslacht` (`Mannelijk`/`Vrouwelijk`), `Academische titel` (zorgverlener);
`Naam`, `Handelsnaam` (onderneming); `Naam`, `Handelsnaam 1`, (`Handelsnaam 2`, …)
(vestiging — handelsnamen are numbered, so collect by label prefix). Labels that
have no value are simply not rendered (the live ended zorgverlener had no
`Academische titel`).

**Contactgegevens** — same block as Basisregistratie, after
`<hr class="border-top my-md-8">`, introduced by `header > h2.h3.mb-0`
"Contactgegevens". Three sub-blocks, each `div.d-flex.gap-2 > span.h5 > svg` +
`div.w-100` whose first child `div.mb-2.h5` is the label:

- `Adresgegevens`: `div.row.g-4.gy-md-8 > div.col-*`, each with
  `h3.h6.mb-2` = soort (`Bezoekadres`, `Correspondentieadres`) and one
  `p.mb-0` with `<br>`-separated lines: line 1 `straat`, line 2
  `"<postcode>, <plaats>"`, line 3 `"<provincie>, <land>"`. Split on `<br>`, then
  on the comma.
- `E-mail`: entries `div > h4.h6.mb-2` (soort, e.g. `Algemeen`) +
  `div.text-nowrap` (value).
- `Telefoonnummer`: identical shape.

**Bevoegdheden** — block `div.block-pt-lg.block-pb-lg.bg-primary-subtle`,
`h2.h3` "Bevoegdheden". Two sub-headings `h3.h4`: `Mijn kwalificaties` and
`Mijn erkenningen`, each followed by `div.row.g-4` of
`div.col-* > div.card > div.card-body`:

- title `h3.h5` — kwalificatie: `"<naam> <code>"`, code appended with a space
  (`Huisarts 0101`, `Huisartsenpraktijk 0100`); erkenning: just the name
  (`RIBIZ Artsen huisartsgeneeskunde`, `Inschrijving handelsregister`,
  `WTZA meldplicht`).
- `dl.mb-0` pairs inside `div.row.g-4.cols-sm-2 > div.col`: `Start`, `Einde`
  (`DD-MM-YYYY` or `-`), and for some erkenningen a leading number column whose
  label is the number kind — `KvK-nummer` observed (`84725486` on an onderneming,
  zero-padded `000007198795` on a vestiging). No number appeared on the
  zorgverlener's erkenning; a `BIG-nummer` label is **not verified** (the fixture
  includes one by analogy — treat the label set as open and key on "the `dt` that
  is not Start/Einde").

**Relaties** — every relation list is one `table.card-table.table.table-striped`
with `data-default-sort-column-index`, inside `div.card-table-container`, preceded
by `div.card-table-header > span.card-table-header__title`. Scope each table by its
`caption.visually-hidden` (stable, exact):

| Caption | Type | Columns |
| --- | --- | --- |
| `Ik ben werkzaam bij de volgende vestigingen` | zorgverlener → vestigingen | Naam, Zorgaanbod, AGB-code, Start, Einde |
| `Ik heb een arbeidsrelatie met` | zorgverlener → arbeidsrelaties (ondernemingen) | Naam, Rol, AGB-code, Start, Einde |
| `Deze onderneming heeft de volgende vestigingen` | onderneming → vestigingen | Naam, Adres, AGB-code, Start, Einde |
| `Bij deze onderneming werken de volgende zorgverleners` | onderneming → zorgverleners | Naam, Rol, AGB-code, Start, Einde |
| `Deze onderneming heeft een relatie met de volgende ondernemingen` | onderneming → ondernemingen | Naam, Type, AGB-code, Start, Einde |
| `Is vestiging van onderneming` | vestiging → parent onderneming (single row) | Naam, AGB-code, Start, Einde |
| `Werkzaam als zorgverlener` | vestiging → zorgverleners | Naam, Zorgaanbod, AGB-code, Start, Einde |

Row cells: `td.card-table__cell.card-table__cell--name > a[href]` (name + URL, so
the related record's type and code come from the href),
`td.card-table__cell--zorgaanbod` (with `data-search` repeating the value and a
`span[title]` wrapper) or a plain `td` for Rol/Type, an AGB-code `td` that is `-`
when the target is a vestiging, and two `td.card-table__cell--date` with
`data-order` epoch (text `DD-MM-YYYY` or `-`).

Nested-table / duplicate-DOM hazards:

- The three onderneming relation tables live in **one** `div.block-pt-lg`, so
  scope by caption (or by the table element), never "all rows in the block".
- On a vestiging page the parent-onderneming relation is rendered **twice**: a
  mobile-only `div.d-md-none > ul > li > div.card-table-card` (with
  `dl.card-table-card__body` and labels `AGB-code:`, `Start:`, `Einde:` — note the
  trailing colons) and the desktop `div.card-table-container.d-none.d-md-block`
  table. Parse the table and ignore `.d-md-none`, or the relation is duplicated.
  This card variant appeared only for that one relation today.
- `dl > dt/dd` scraping must be scoped per section: Basisregistratie,
  Bevoegdheden cards and the vestiging relation card all use `dl`s.

Empty vs absent sections: **absent** means the whole construct is missing (no
`Contactgegevens` heading at all on a zorgverlener page). A present-but-empty
section was not observed live; the markup shows the heading
(`div.mt-4.mt-md-8 > h3.h4`) followed by an empty `div.row.g-4`, so treat
"heading present, zero cards/rows" as empty and "no heading" as absent.

## 4. Session expiry

`POST /agb-register/zoeken/resultaten` with a valid-looking but stale `_token` and
no cookies → **`419`**, `Content-Type: text/html`, 6 665 bytes, Laravel's default
page: `<title>Page Expired</title>`, `<h1 …>419</h1>`, `Page Expired`. No
`Location`. This is exactly the condition the design restarts the transaction on
(one retry with a fresh form GET). A 403-with-expiry-page variant was not
observed.

## 5. Findings that changed the original plan

1. Cap detection is better than assumed: there **is** an explicit marker
   (`#500PlusAlert`, "Er zijn meer dan 500 resultaten: niet alle resultaten worden
   getoond"), so a full 500-row page without a count need not be indeterminate.
2. The count heading has **no thousands separator** today.
3. `kwalificaties[]` is the field name and the encoding is a repeated bracketed
   key; `plaats`, `postcode`, `kvknummer` are plain names; `plaats` is a substring
   match and `postcode` a prefix match. Multi-value kwalificatie semantics
   (OR vs AND) remain unverified.
4. Two `select name="zorgsoort"` elements exist; only the enabled one is
   submitted. Send a single value.
5. Both vestiging URL forms return `200` with no redirect, so **three** candidate
   URLs suffice (`zorgverlener-`, `onderneming-`, `vestiging-<digits>`).
6. A vestiging detail page never shows its AGB-code (header shows `-`). The
   design's "every fetched detail page must show the requested code" cannot hold
   for vestigingen: verify the type from the breadcrumb/header label and the code
   from the requested URL, and treat a `-` code as expected for that type.
7. A zorgverlener page's `h1` is empty; take the name from Basisregistratie.
8. Nonexistent **and** wrong-type URLs both yield a plain `404` with the same
   rendered page — no redirect to the form.
9. `vektis-mcp/0.1` is served normally; the Chrome UA fallback is unnecessary.
10. reCAPTCHA v3 is mounted but still unenforced; omit `g-recaptcha-response`.

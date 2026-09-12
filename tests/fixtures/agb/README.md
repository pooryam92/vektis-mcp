# AGB HTML fixtures

Synthetic, commit-safe reductions of live pages from
`https://www.vektis.nl/agb-register/`, observed **2026-09-12**. See
[docs/agb-source-notes.md](../../../docs/agb-source-notes.md) for the full
verification record (status codes, form fields, selectors).

Every fixture keeps the tags, classes, attributes and Dutch text labels the
parsers key on, and drops head metadata, navigation, footers, scripts, styles and
icons. **All personal data is replaced.** No raw response is committed: the live
pages contain names, addresses, e-mail addresses and phone numbers of identifiable
people.

## Synthetic identities used everywhere

| Kind | Value |
| --- | --- |
| Zorgverlener codes | `01999001`, `01999002`, `01999009` (ended), `01999010` (ended) |
| Onderneming codes | `01999100`, `01999101` |
| Vestiging codes | `71999001` → slug `4e7a45354f546b774d44453d`, `71999002` → slug `4e7a45354f546b774d44493d` (`hex(base64(code))`, verified consistent) |
| Person names | `A Testpersoon` … `D Testpersoon` (search rows), `A. Testpersoon` / `B. Testpersoon` (detail), geboortenaam `A. Testpersoon-Voorbeeld` |
| Organisation names | `Praktijk Voorbeeld B.V.`, `Praktijk Voorbeeld Noord B.V.`, handelsnamen `Praktijk Voorbeeld`, `Huisartsenpraktijk Voorbeeld` |
| Addresses | `Teststraat 1` / `Teststraat 2` / `Postbus 1`, postcodes `1234AB` / `1234CD` (source format: upper-case, no space), `Teststad` / `TESTSTAD`, `Testprovincie`, `Nederland` |
| E-mail / phone | `info@praktijkvoorbeeld.example`, `praktijk@voorbeeld.example`, `0101234567` |
| Numbers | KvK `12345678` and zero-padded `000012345678`, BIG `12345678901` |
| CSRF tokens | `TESTFORMTOKEN000…` (form input), `TESTCSRFMETATOKEN…` (head meta) |
| Dates | real-looking but arbitrary: `01-07-1988`, `09-12-2021`, `04-03-2024`, `2024-12-31` |

Kwalificatie, erkenning, zorgsoort and relation labels are **real register
labels** (`Huisarts 0101`, `Huisartsenpraktijk 0100`, `RIBIZ Artsen
huisartsgeneeskunde`, `Inschrijving handelsregister`, `WTZA meldplicht`,
`Vrijgevestigd (MTO getekend)`, `Eigenaar`, `Samenwerkingsverband`) — they are
code-table values, not personal data, and the parsers key on them.

## Files

| File | Source observation | Synthesised / trimmed |
| --- | --- | --- |
| `search_form.html` | `GET /agb-register/zoeken` → 200 | Keeps `_token`, both `zorgpartijtype` radios, `agbcode`, both `zorgsoort` selects (the organisation one `hidden disabled`), `#frmKwalificaties`, `plaats`/`postcode`/`kvknummer` in `fieldset#tab2`, the `g-recaptcha-response` hidden input, both `<template>`s (including `name="kwalificaties[]"`) and the inline `const qualifications = {…}` script. Option lists cut to 2–3 zorgsoorten per side and the JSON to 2 zorgsoorten per side; token replaced. |
| `search_results_zorgverlener.html` | search #1 (`zorgpartijtype=zorgverlener`) | 4 rows (2 active, 2 with an einddatum), heading `4 Zoekresultaten` matching the rows. Full `thead`/`tfoot` kept so the DataTables filter inputs in `tfoot` stay in the way of naive parsers. |
| `search_results_organisatie.html` | searches #2/#3 (`onderneming,vestiging`) | 4 rows: 2 ondernemingen with plain codes, 2 vestigingen with `-` in the AGB-code column and an encoded slug href; addresses on 3 rows, one onderneming row blank+ended; heading `4 Zoekresultaten`. |
| `search_results_empty.html` | search #6 (`agbcode=00000000`) | Only `section.js-search-results` with the `alert-info` "Jouw zoekopdracht leverde geen resultaten op…" text. No `#resultsContainer`, no heading, no table — as live. |
| `search_results_capped.html` | search #4 (`agbcode=01`, 15857 hits / 500 rows) | Real cap marker `div.alert-warning#500PlusAlert` with the exact text; heading set to `800 Zoekresultaten` with only 3 rows so count ≫ rows. |
| `record_zorgverlener.html` | `GET /agb-register/zorgverlener-<code>` → 200 (active) | Header (empty `h1`, type text node, constant `Organisatie` label, code, `Start:`/`Einde:` spans), Basisregistratie (Naam, Geboortenaam, Geslacht, Academische titel), Bevoegdheden (2 kwalificatie cards incl. one ended, 2 erkenning cards), Relaties (`Ik ben werkzaam bij de volgende vestigingen`, `Ik heb een arbeidsrelatie met`). No Contactgegevens — individuals have none. The second erkenning card (`BIG-registratie` + `BIG-nummer`) is **constructed by analogy** with the onderneming's `KvK-nummer` card; no live zorgverlener erkenning with a number was observed. |
| `record_zorgverlener_ended.html` | `GET /agb-register/zorgverlener-<code>` → 200 (ended) | Same page plus the real ended marker `div.alert.warning` … `&nbsp;Deze AGB-code is beëindigd` before `<header>`; header `Einde: 04-03-2024`, end dates on kwalificaties/erkenningen/relations, `Academische titel` dropped (it is simply absent when empty). |
| `record_onderneming.html` | `GET /agb-register/onderneming-<code>` → 200 | Basisregistratie (Naam, Handelsnaam), Contactgegevens (Bezoekadres + Correspondentieadres as `<br>`-separated `p`, E-mail, Telefoonnummer), Bevoegdheden (1 kwalificatie, `Inschrijving handelsregister` with `KvK-nummer`, `WTZA meldplicht`), and all three relation tables (`Vestigingen`, `Zorgverleners`, `Ondernemingen`) in one block. |
| `record_vestiging.html` | `GET /agb-register/vestiging-<slug>` → 200 | Header with `-` as the displayed AGB-code (live behaviour: a vestiging page never shows its own code) and the requested URL echoed in `meta[itemprop=url]`/`og:url`; Basisregistratie with numbered `Handelsnaam 1`/`Handelsnaam 2`; Contactgegevens with one address; the parent-onderneming relation rendered **twice** as live (mobile `div.d-md-none` `card-table-card` + desktop `div.card-table-container.d-none.d-md-block` table); Bevoegdheden; `Relaties` → `Werkzaam als zorgverlener`. |
| `record_not_found.html` | `GET /agb-register/zorgverlener-99999999` → **404** (identical for a wrong-type URL such as `onderneming-<zorgverlener code>`) | The rendered 404 body only: `h1.h3` "De pagina die je zoekt is niet gevonden" plus the site-search form. Nothing synthesised; no redirect is involved. |
| `session_expired.html` | `POST /agb-register/zoeken/resultaten` with a stale `_token` and no cookies → **419** | Laravel's default "Page Expired" page, as observed (not reconstructed); only the inline Tailwind `<style>` block was dropped. |

Useful invariants for tests: search fixtures parse with the legacy
`vektis_agb.RowParser`/`parse_count`; `search_results_*` headings match their row
counts except `search_results_capped.html` (800 vs 3, deliberately); vestiging
slugs in the fixtures decode to the `71999xxx` codes above.

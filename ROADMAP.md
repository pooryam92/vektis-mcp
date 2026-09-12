# Roadmap

## Now: AGB-register — done

All four tools are implemented and covered by an offline test suite; see the
[design](docs/design.md) and the live
[source notes](docs/agb-source-notes.md).

- `agb_search_zorgverleners`, `agb_search_organisaties` — paced source client, hardened row parser, render-cap and ended-record handling.
- `agb_get_record` — direct detail probes with verified page identity, recognized absence, cross-type ambiguity and section selection for zorgverlener, onderneming and vestiging.
- `agb_lookup_codes` — zorgsoorten and kwalificaties by code or label from `data/codes.json`, no network.
- Stdio by default, Streamable HTTP on request, settings validated at startup.

Open follow-ups: confirm whether multiple `kwalificaties[]` values are OR-ed or
AND-ed upstream, exercise `kvknummer` live, and run the periodic live smoke check
that is deliberately outside CI.

## Next: other public Vektis sources

Ranked by value and effort. All verified public, no account needed.

| Tool | Source | Params | Notes |
| --- | --- | --- | --- |
| `zorgaanbod_stats` | [zorgaanbod/dashboard](https://www.vektis.nl/zorgaanbod/dashboard) | `zorgtype`, `gemeente` | Vestigingen and zorgverleners per gemeente and landelijk, with peildatum. Plain GET, e.g. `?zorgtype=Huisartsen:huisarts&locatietype=Gemeenten:assen`. Bracketed array params return a 500. |
| `tog_tarief` | [tog.vektis.nl/TogZoeken.aspx](https://tog.vektis.nl/TogZoeken.aspx) | `declaratiecode`, `peildatum`, `agbcode?` | Tariff, description and codelijst for a prestatiecode. ASP.NET WebForms, needs viewstate round-trip. |
| `prestatiecode_zoek` | [TOG prestatiecodelijsten](https://tog.vektis.nl/WebInfo.aspx?ID=Prestatiecodelijsten) | `lijst`, `query` | One xlsx per list (008 huisartsen, 010 mondzorg, 012 paramedie, ...). Download and cache. |
| `uzovi_zoek`, `uzovi_get` | [uzovi-register](https://www.vektis.nl/uzovi-register) | `query`, `rol?`, `include_ended?` / `code` | Single xlsx with insurers, labels, relaties and concerns. Updated a few times a year. |
| `codelijst_get`, `codelijst_zoek` | [standaardisatie/codelijsten](https://www.vektis.nl/standaardisatie/codelijsten) | `id` / `query` | ~150 lists (CL0001-VEKT, COD016-VEKT, ...). Each page is an HTML table of code, betekenis, validity. |
| `zvw_kosten` | [open databestanden](https://www.vektis.nl/zorgprisma/open-databestanden) | `jaar`, `niveau`, `gemeente` or `pc3` | CSV per year 2011–2024, costs per category by gender and age. Non-commercial use, cite Vektis. |
| `vektis_zoek` | `/api/search?q=` | `query` | Site search, JSON. Cheap. |

Suggested order: `zorgaanbod_stats` and `tog_tarief` first (they complement the AGB tools directly), then the static files (UZOVI, codelijsten), then open data.

## Not planned

Needs a contract or Vektis account: AGB Raadpleegdienst, UBO, RIZ, IFM, TOGBEST, Zorgprisma dashboards. ISI-register has no public list yet.

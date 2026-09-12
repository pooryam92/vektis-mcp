# Tool reference

The contract an MCP client can rely on. Schemas are defined in
`vektis_mcp/models.py` and the tool signatures in `vektis_mcp/server.py`.
Field names follow the register's own Dutch labels. Codes are strings so leading
zeros survive.

## Errors

Every failure is an MCP tool error, never an empty result or a `not_found`
record. Input errors carry a plain message. Source-side failures are prefixed so
a client can tell them apart without parsing prose:

| Prefix | Meaning |
| --- | --- |
| `source_unavailable:` | Network failure, timeout, exhausted retries or the call deadline |
| `source_blocked:` | The source answered 403 or a challenge page |
| `parse_error:` | The page was fetched but not understood, or its identity did not match |

## `agb_search_zorgverleners`

```text
agb_search_zorgverleners(query, zorgsoort?, kwalificaties?, include_ended=false, limit=25)
```

Searches individual care providers. `query` is required: digits match the
AGB-code as a substring, letters match the name. `zorgsoort` is a two-digit code
from the zorgverlener table (the enum on the tool lists every code with its
label). `kwalificaties` is a list of four-digit codes, validated against the
side and, when given, the zorgsoort.

## `agb_search_organisaties`

```text
agb_search_organisaties(query?, plaats?, postcode?, kvknummer?, zorgsoort?, kwalificaties?, include_ended=false, limit=25)
```

Searches ondernemingen and vestigingen. At least one of `query`, `plaats`,
`postcode`, `kvknummer`, `zorgsoort` or `kwalificaties` must be non-empty;
`include_ended` and `limit` do not count. `plaats` matches as a case-insensitive
substring on the source, so "Assen" also matches Wassenaar. `postcode` is a
prefix match, four digits optionally followed by two letters, normalized to
upper case without a space. `kvknummer` is up to twelve digits.

## Search response

Both search tools return the same shape:

| Field | Meaning |
| --- | --- |
| `results` | Candidates, not confirmed identities. Each has `record_type`, `naam`, `agbcode`, `adres`, `postcode`, `plaats`, `einddatum` and `source_url`. Names of individuals are initials only. |
| `returned_count` | Rows returned after local filtering and the limit |
| `source_total` | The source's own match count before local filtering, or null when the page had none |
| `applied_filters` | The normalized inputs that were actually sent |
| `has_more` | More matching rows were seen and dropped at `limit` |
| `source_truncated` | The source's 500-row render cap hid matches that were never seen. Narrow the search. |

Neither flag implies pagination; there is none. `limit` ranges from 1 to 500.
Location filters apply only to organisations.

## `agb_get_record`

```text
agb_get_record(agbcode, record_type?, sections?)
```

Looks up one exact registration by its eight-digit code. `record_type` is
`zorgverlener`, `onderneming` or `vestiging`; pass the value from search. When
omitted, every type is probed and the response never silently picks the first
match.

| `outcome` | `record` | `candidates` |
| --- | --- | --- |
| `found` | the record | empty |
| `not_found` | null | empty |
| `ambiguous` | null | one entry per matching type; retry with `record_type` |

`sections` selects detail sections: `basisregistratie`, `contact`,
`kwalificaties`, `erkenningen`, `relaties`. Omitted means basisregistratie and
kwalificaties; an empty list means identity and status only.

A found record always carries `record_type`, `agbcode`, `naam`, `status`
(`start`, `einde`, `beeindigd`), `source_url`, a timezone-aware `retrieved_at`,
`requested_sections` and `unavailable_sections`. Section fields are null when
not requested or unavailable. `unavailable_sections` lists requested sections
with a known expected absence; today that is `contact` on individual providers.
Any other missing requested section is a `parse_error`, because a renamed or
removed section cannot be told apart from missing data. An empty list in an
available section means no entries. `retrieved_at` is when the page was fetched,
not when the register was updated.

## `agb_lookup_codes`

```text
agb_lookup_codes(entity_kind, zorgsoort?, query?)
```

Reads the bundled code tables; no network. `entity_kind` is `zorgverlener` or
`organisatie`.

| Inputs | Result |
| --- | --- |
| none | all zorgsoorten |
| `zorgsoort` | its kwalificaties |
| `query` | zorgsoorten and kwalificaties whose code or label contains it, case-insensitive |
| both | kwalificaties under that zorgsoort matching the query |

Each match has `kind`, `code`, `naam` and `parent_code` (null for a zorgsoort).
An unknown `zorgsoort` is an input error; an unmatched query returns an empty
list.

## Resource `vektis://codes/{side}`

The full code table for one side (`zorgverlener` or `organisatie`) as JSON, the
same data the lookup tool reads.

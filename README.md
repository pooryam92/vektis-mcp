# Vektis MCP

<!-- mcp-name: io.github.pooryam92/vektis-mcp -->

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/protocol-MCP-black)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

MCP server for the public [AGB-register](https://www.vektis.nl/agb-register) of
Vektis: search Dutch healthcare providers, practices and locations by AGB-code,
name, zorgsoort, place or postcode, and read a full registration with its
kwalificaties, erkenningen and relations. No API key, no account.

> Unofficial. This project is not affiliated with or endorsed by Vektis. Every
> tool call reads the public register live; nothing is stored.

**Voor Nederlandse gebruikers:** zoek zorgverleners, ondernemingen en vestigingen
in het AGB-register vanuit Claude, Cursor of een andere MCP-client. Stel vragen
in het Nederlands; de veldnamen in de antwoorden zijn dezelfde als op vektis.nl.

## Quick start

You need [uv](https://docs.astral.sh/uv/) and internet access. The package is
not on PyPI yet, so `uvx` installs it straight from GitHub.

**Claude Desktop**: add to `claude_desktop_config.json` and restart.

```json
{
  "mcpServers": {
    "vektis": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/pooryam92/vektis-mcp", "vektis-mcp"]
    }
  }
}
```

**Claude Code**:

```sh
claude mcp add vektis -- uvx --from git+https://github.com/pooryam92/vektis-mcp vektis-mcp
```

**Other clients** use the same `command` and `args` under their own top-level key:

| Client | File | Key |
| --- | --- | --- |
| Cursor | `~/.cursor/mcp.json` | `mcpServers` |
| VS Code | `.vscode/mcp.json` | `servers` (add `"type": "stdio"`) |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | `mcpServers` |

<details>
<summary>From a local checkout</summary>

```sh
git clone https://github.com/pooryam92/vektis-mcp
cd vektis-mcp
uv sync
uv run vektis-mcp
```

In a client config, point `command` at `uv` with
`"args": ["--directory", "/absolute/path/to/vektis-mcp", "run", "vektis-mcp"]`.
`pip install .` followed by `vektis-mcp`, or `python -m vektis_mcp`, works too.

</details>

<details>
<summary>Over HTTP</summary>

```sh
uvx --from git+https://github.com/pooryam92/vektis-mcp vektis-mcp --transport streamable-http --host 127.0.0.1 --port 8000
claude mcp add --transport http vektis http://127.0.0.1:8000/mcp
```

The server binds to localhost. Before exposing it further, put HTTPS,
authentication, Origin validation and request limits in front of it. Run one
process: request pacing is per process.

</details>

## Example prompts

- "Zoek huisartsenpraktijken in Assen."
- "Wat is AGB-code 01000451 en is die nog actief?"
- "Welke zorgverleners werken bij vestiging 71001001?"
- "Geef de contactgegevens en erkenningen van onderneming 01010001."
- "Find dental practices with postcode 9403 and list their KvK-nummer."
- "Which kwalificatie codes exist for zorgsoort 01?"

A typical flow is search, then `agb_get_record` with the `record_type` from the
result. Search results are candidates, not confirmed identities.

## Tools

| Tool | What it does |
| --- | --- |
| `agb_search_zorgverleners` | Individual care providers by AGB-code or name, filtered by zorgsoort and kwalificaties |
| `agb_search_organisaties` | Ondernemingen and vestigingen by code or name, plus plaats, postcode and kvknummer |
| `agb_get_record` | One exact registration with optional sections: basisregistratie, contact, kwalificaties, erkenningen, relaties |
| `agb_lookup_codes` | Zorgsoort and kwalificatie codes by code or label, offline |

The code tables are also served as the resource `vektis://codes/{side}`. Full
parameter and response semantics are in [docs/tools.md](docs/tools.md).

## Terminology

| Term | Meaning |
| --- | --- |
| AGB-code | Eight-digit identifier, leading zeros included. People and businesses can share a `01` prefix, so a code alone is not always unique across types. |
| Zorgverlener | An individual care provider (huisarts, tandarts, fysiotherapeut, ...). Search shows initials only and no address. |
| Onderneming | The legal entity: a practice, hospital or company. |
| Vestiging | A location of an onderneming. Its code appears only in the page URL, never on the page itself. |
| Zorgsoort | Two-digit care category, e.g. `01` huisartsen. |
| Kwalificatie | Four-digit qualification under a zorgsoort, e.g. `0101` huisarts. |
| Erkenning | A recognition such as a BIG or KvK registration or a WTZA notification. |

## Configuration

All optional, set as environment variables (the `env` block of a client config).

| Variable | Default | Purpose |
| --- | --- | --- |
| `VEKTIS_REQUEST_INTERVAL_SECONDS` | `1.0` | Minimum interval between requests to vektis.nl |
| `VEKTIS_HTTP_TIMEOUT_SECONDS` | `20` | Per-request timeout |
| `VEKTIS_TOOL_DEADLINE_SECONDS` | `60` | Total budget per tool call, including pacing and retries |
| `VEKTIS_MAX_RETRIES` | `2` | Retries after the first attempt |
| `VEKTIS_USER_AGENT` | a generic Chrome string | User-Agent sent to Vektis |

Bad values fail at startup. By default the client sends the headers of an
ordinary browser visit, so the traffic does not stand out; set
`VEKTIS_USER_AGENT` if you want to identify your deployment instead.

## Data source and limits

- The source is the public register at vektis.nl, read as HTML. There is no
  official API behind this server, so a site redesign can break parsing; such
  failures surface as `parse_error` tool errors, never as empty results.
- The register renders at most 500 rows per search. `source_truncated` in the
  response tells you the cap was hit; narrow the search by zorgsoort, plaats or
  postcode.
- Requests are paced and retried with backoff to stay polite to Vektis. One tool
  call is one to four live requests.
- The register contains names and contact details of care professionals. Use
  the data within Vektis's terms of use and applicable privacy law.
- Only the AGB-register is covered today. Other public Vektis sources are listed
  in [ROADMAP.md](ROADMAP.md).

## Development

```sh
uv sync
uv run pytest -q
npx @modelcontextprotocol/inspector uv run vektis-mcp
```

The test suite runs fully offline against synthetic fixtures. How the server is
built is described in [docs/design.md](docs/design.md); what the live site
does, field by field, in [docs/agb-source-notes.md](docs/agb-source-notes.md).

## License

MIT for the code, see [LICENSE](LICENSE). The register data belongs to Vektis
and is subject to the terms of use on vektis.nl.

## More Dutch extensions

For more Dutch extensions for your AI, see [qontex.nl](https://qontex.nl).

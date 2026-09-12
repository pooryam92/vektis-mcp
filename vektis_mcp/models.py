"""Pydantic schemas for the Vektis AGB-register MCP tools.

Field names follow the register's own Dutch labels so results map 1:1 onto
what the site shows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# Which side of the register. `organisatie` is the site's combined
# `onderneming,vestiging` value.
Side = Literal["zorgverlener", "organisatie"]

# The three kinds of detail page. `01…` codes are used by both zorgverleners
# and ondernemingen, so a code alone does not always identify the page.
RecordType = Literal["zorgverlener", "onderneming", "vestiging"]
RecordSection = Literal["basisregistratie", "contact", "kwalificaties", "erkenningen", "relaties"]


# --- agb_search ------------------------------------------------------------


class SearchResult(BaseModel):
    """Compact candidate for an exact detail lookup."""

    record_type: RecordType
    naam: str = Field(description="Initials-only for persons, e.g. 'A Booij'")
    agbcode: str = Field(description="Eight digits; vestigingen decoded from the URL slug")
    adres: str = ""
    postcode: str = ""
    plaats: str = ""
    einddatum: str = Field("", description="ISO date when deregistered, else empty")
    source_url: str


class AppliedFilters(BaseModel):
    query: str | None = None
    zorgsoort: str | None = None
    kwalificaties: list[str] | None = None
    plaats: str | None = None
    postcode: str | None = None
    kvknummer: str | None = None
    include_ended: bool = False


class SearchResponse(BaseModel):
    results: list[SearchResult]
    returned_count: int = Field(ge=0, description="Number of returned results after local filtering and limit.")
    source_total: int | None = Field(None, ge=0, description="Source match count before local filtering; null when unavailable.")
    applied_filters: AppliedFilters
    has_more: bool = Field(description="More matching rows are known to exist beyond the returned limit.")
    source_truncated: bool = Field(description="Source render cap prevents inspecting all matches; narrow the search.")


# --- agb_get_record --------------------------------------------------------


class Period(BaseModel):
    start: str = ""
    einde: str = ""


class Status(Period):
    beeindigd: bool = Field(description="True when the page says 'Deze AGB-code is beëindigd'")


class Basisregistratie(BaseModel):
    naam: str
    geboortenaam: str = ""
    geslacht: str = ""
    academische_titel: str = ""
    handelsnamen: list[str] = []


class Adres(BaseModel):
    soort: str = Field(description="'Bezoekadres' or 'Correspondentieadres'")
    straat: str = ""
    postcode: str = ""
    plaats: str = ""
    provincie: str = ""
    land: str = ""


class Contact(BaseModel):
    adressen: list[Adres] = []
    email: str = ""
    telefoon: str = ""


class Kwalificatie(Period):
    code: str = Field(description="e.g. '0101' (Huisarts) or '0100' (Huisartsenpraktijk)")
    naam: str


class Erkenning(Period):
    naam: str = Field(description="e.g. 'RIBIZ Artsen huisartsgeneeskunde', 'Inschrijving handelsregister', 'WTZA meldplicht'")
    nummer: str = Field("", description="KvK- or BIG-nummer when the erkenning carries one")


class Relatie(Period):
    naam: str
    agbcode: str = ""
    rol: str = Field("", description="Rol / zorgaanbod / type column, whichever the table has")
    adres: str = ""


class Relaties(BaseModel):
    vestigingen: list[Relatie] = []
    zorgverleners: list[Relatie] = []
    ondernemingen: list[Relatie] = []
    arbeidsrelaties: list[Relatie] = []
    onderneming: Relatie | None = Field(None, description="For a vestiging: the onderneming it belongs to")


class Record(BaseModel):
    record_type: RecordType
    agbcode: str
    source_url: str
    retrieved_at: datetime = Field(description="Time the source was fetched, with timezone; not the registry update time.")
    status: Status
    naam: str
    requested_sections: list[RecordSection]
    unavailable_sections: list[RecordSection] = Field(default_factory=list, description="Requested sections with a known expected absence (contact on individual providers). Unexpected missing sections are parse errors. Empty lists in available sections mean no entries.")
    basisregistratie: Basisregistratie | None = None
    contact: Contact | None = None
    kwalificaties: list[Kwalificatie] | None = None
    erkenningen: list[Erkenning] | None = None
    relaties: Relaties | None = None


class RecordResponse(BaseModel):
    outcome: Literal["found", "not_found", "ambiguous"]
    record: Record | None = None
    candidates: list[SearchResult] = Field(default_factory=list, description="Alternatives for an ambiguous lookup; retry with record_type.")


# --- agb_lookup_codes -------------------------------------------------------


class CodeMatch(BaseModel):
    kind: Literal["zorgsoort", "kwalificatie"]
    code: str = Field(description="Two-digit zorgsoort or four-digit kwalificatie code; preserve leading zeros.")
    naam: str
    parent_code: str | None = Field(None, description="Parent zorgsoort for a kwalificatie; null for a zorgsoort.")


class CodeLookupResponse(BaseModel):
    entity_kind: Side
    results: list[CodeMatch]

"""Static code tables scraped from the AGB-register search form.

The form embeds a `qualifications` JSON blob keyed by side and zorgsoort; this
module is generated from it (see data/codes.json). Regenerate when Vektis adds a
zorgsoort.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Sequence
from importlib.resources import files
from typing import Literal

from .agb.errors import InvalidInput
from .models import CodeLookupResponse, CodeMatch, Side

ZorgsoortZorgverlener = Literal[
    "01",
    "02",
    "03",
    "04",
    "05",
    "07",
    "08",
    "11",
    "12",
    "13",
    "14",
    "24",
    "26",
    "33",
    "44",
    "57",
    "84",
    "87",
    "88",
    "89",
    "90",
    "91",
    "93",
    "94",
    "96",
]

ZorgsoortOrganisatie = Literal[
    "01",
    "02",
    "03",
    "04",
    "05",
    "06",
    "07",
    "08",
    "11",
    "12",
    "13",
    "14",
    "17",
    "18",
    "19",
    "20",
    "21",
    "22",
    "23",
    "24",
    "25",
    "26",
    "30",
    "31",
    "32",
    "33",
    "34",
    "35",
    "36",
    "37",
    "38",
    "39",
    "40",
    "41",
    "42",
    "43",
    "44",
    "45",
    "46",
    "47",
    "48",
    "49",
    "50",
    "51",
    "52",
    "53",
    "54",
    "56",
    "58",
    "60",
    "61",
    "65",
    "66",
    "67",
    "70",
    "72",
    "73",
    "74",
    "75",
    "76",
    "78",
    "79",
    "84",
    "85",
    "87",
    "88",
    "89",
    "90",
    "91",
    "93",
    "94",
    "96",
    "98",
]

ZORGSOORT_ZORGVERLENER_DESC = (
    "Two-digit zorgsoort code. Omit for all. "
    "01 Huisartsen | 02 Apothekers | 03 Medisch Specialisten | 04 Fysiotherapeuten | 05 Logopedisten | 07 Oefentherapeuten | 08 Verloskundigen | 11 Tandarts - Specialisten (Mondziekten en Kaakchirurgie) | 12 Tandartsen | 13 Tandarts - Specialisten (Dento - Maxillaire Orthopedie) | 14 Bedrijfsartsen | 24 Dietisten | 26 Podotherapeuten | 33 Kraamzorg | 44 Optometristen | 57 Physician Assistant | 84 Overige Artsen | 87 Mondhygienisten | 88 Ergotherapeuten | 89 Schoonheidsspecialisten | 90 Overige therapeuten en Complementair en Aanvullende zorg | 91 Verpleegkundigen | 93 Tandtechnici / Tandprothetici | 94 Psychologische Zorgverleners | 96 Pedicuren"
)

ZORGSOORT_ORGANISATIE_DESC = (
    "Two-digit zorgsoort code. Omit for all. "
    "01 Huisartsen | 02 Apothekers | 03 Medisch Specialisten | 04 Fysiotherapeuten | 05 Logopedisten | 06 Ziekenhuizen | 07 Oefentherapeuten | 08 Verloskundigen | 11 Tandarts - Specialisten (Mondziekten en Kaakchirurgie) | 12 Tandartsen | 13 Tandarts - Specialisten (Dento-Maxillaire Orthopedie) | 14 Bedrijfsartsen | 17 Rechtspersonen | 18 Dialyse Centra | 19 Audiologische Centra | 20 Radiotherapeutische Centra | 21 Dienstenstructuren (ANW-Diensten) | 22 Zelfstandige Behandelcentra | 23 Instellingen voor Revalidatiedagbehandeling | 24 Dietisten | 25 Instellingen voor Psychiatrische Deeltijdbehandeling | 26 Podotherapeuten | 30 Instellingen voor Verstandelijk Gehandicapten | 31 Bloedbanken | 32 GGD | 33 Kraamzorg | 34 Trombosediensten | 35 Instellingen voor Visueel Gehandicapten | 36 Ambulancediensten | 37 Gezondheidscentra | 38 Tandheelkundige Centra | 39 Instellingen voor Jeugdtandverzorging | 40 Instellingen voor Auditief Gehandicapten | 41 ZZP-ers in wijkverpleging/ PGB aanbieders / Beheerstichtingen | 42 Verzorgingshuizen | 43 Beheerstichtingen Verzorgingstehuizen | 44 Optometristen | 45 Verpleeginrichtingen voor Somatische Ziekten | 46 Verpleeginrichtingen voor Psycho-Geriatrische Patienten | 47 Gecombineerde Verpleeginrichtingen | 48 Overige Instellingen | 49 Abortusklinieken | 50 Laboratoria(Huisartsenlab./Gemeensch.Lab/Gemeensch Apoth+Lab | 51 Klinisch-Genetische Centra | 52 Eurotransplant | 53 Diverse Samenwerkingsverbanden | 54 GGZ Instellingen (PUK/PAAZ) | 56 Consultatiebureaus voor Alcohol en Drugs | 58 Centrale Post Ambulancediensten - CPA | 60 Instellingen voor Dagverpleging voor Ouderen | 61 Beheerstichtingen Dagverblijven | 65 Gezinsvervangende Tehuizen | 66 Koepels en Beheerstichtingen WLZ | 67 Netwerk organisaties | 70 Kinderdagverblijven | 72 RIBW | 73 WLZ Gecombineerd | 74 Arbodiensten | 75 Thuiszorginstellingen | 76 Leveranciers Hulpmiddelen | 78 Sociaal Pedagogische Diensten | 79 RIAGG | 84 Overige Artsen | 85 Taxivervoerders | 87 Mondhygienisten | 88 Ergotherapeuten | 89 Schoonheidsspecialisten | 90 Overige therapeuten en Complementair en Aanvullende zorg | 91 Verpleegkundigen | 93 Tandtechnici / Tandprothetici | 94 Psychologische Zorgverleners | 96 Pedicuren | 98 Declaranten/Servicebureaus/Zorgverzekeraars"
)


@functools.lru_cache(maxsize=1)
def load_codes() -> dict:
    """{side: {zorgsoort: {"naam": str, "kwalificaties": {code: naam}}}}"""
    return json.loads(files("vektis_mcp.data").joinpath("codes.json").read_text(encoding="utf-8"))


def side_codes(entity_kind: Side) -> dict:
    """The zorgsoort table for one side, in source order."""
    try:
        return load_codes()[entity_kind]
    except KeyError:
        raise InvalidInput(f"Unknown entity_kind {entity_kind!r}; expected zorgverlener or organisatie.") from None


def validate_zorgsoort(entity_kind: Side, zorgsoort: str | None) -> str | None:
    """Return the zorgsoort unchanged, or None; raise when the side does not have it."""
    if zorgsoort is None:
        return None
    table = side_codes(entity_kind)
    if zorgsoort not in table:
        raise InvalidInput(f"Unknown zorgsoort {zorgsoort!r} for {entity_kind}.")
    return zorgsoort


def validate_kwalificaties(
    entity_kind: Side, zorgsoort: str | None, kwalificaties: Sequence[str] | None
) -> tuple[str, ...]:
    """Clean a kwalificatie list: trimmed, deduplicated in order, known on this side.

    With a zorgsoort, every code must belong to it. An empty or missing list is no
    criterion at all.
    """
    if not kwalificaties:
        return ()
    table = side_codes(entity_kind)
    if zorgsoort is not None:
        validate_zorgsoort(entity_kind, zorgsoort)
        allowed = set(table[zorgsoort]["kwalificaties"])
        scope = f"zorgsoort {zorgsoort}"
    else:
        allowed = {code for entry in table.values() for code in entry["kwalificaties"]}
        scope = entity_kind

    cleaned: list[str] = []
    for raw in kwalificaties:
        code = raw.strip()
        if code and code not in cleaned:
            cleaned.append(code)
    unknown = [code for code in cleaned if code not in allowed]
    if unknown:
        raise InvalidInput(f"Unknown kwalificatie codes for {scope}: {', '.join(unknown)}.")
    return tuple(cleaned)


def lookup_codes(entity_kind: Side, zorgsoort: str | None, query: str | None) -> CodeLookupResponse:
    """List or search zorgsoorten and kwalificaties for one side of the register.

    No filters lists zorgsoorten; a zorgsoort lists its kwalificaties; a query alone
    searches both levels; both searches kwalificaties under that zorgsoort. Matching is
    a casefolded substring of the code or the naam.
    """
    table = side_codes(entity_kind)
    validate_zorgsoort(entity_kind, zorgsoort)
    needle = query.strip().casefold() if query and query.strip() else None

    results: list[CodeMatch] = []
    if zorgsoort is not None:
        results = _kwalificatie_matches(zorgsoort, table[zorgsoort], needle)
    elif needle is None:
        results = [CodeMatch(kind="zorgsoort", code=code, naam=entry["naam"]) for code, entry in table.items()]
    else:
        for code, entry in table.items():
            if _matches(needle, code, entry["naam"]):
                results.append(CodeMatch(kind="zorgsoort", code=code, naam=entry["naam"]))
            results.extend(_kwalificatie_matches(code, entry, needle))
    return CodeLookupResponse(entity_kind=entity_kind, results=results)


def _kwalificatie_matches(zorgsoort: str, entry: dict, needle: str | None) -> list[CodeMatch]:
    """Kwalificaties of one zorgsoort, optionally narrowed to a casefolded substring."""
    return [
        CodeMatch(kind="kwalificatie", code=code, naam=naam, parent_code=zorgsoort)
        for code, naam in entry["kwalificaties"].items()
        if needle is None or _matches(needle, code, naam)
    ]


def _matches(needle: str, code: str, naam: str) -> bool:
    return needle in code.casefold() or needle in naam.casefold()

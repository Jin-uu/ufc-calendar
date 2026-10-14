"""Normalise the country spellings of the data sources to ISO 3166-1 alpha-2 codes.

FightMetric uses three-letter IOC codes (e.g. "GER", "SUI", "BRN" = Bahrain) and ESPN uses
English names (e.g. "South Korea", "England"). Everything is mapped to ISO alpha-2 ("KR").
"""

from __future__ import annotations

import gettext
import logging
from functools import cache

import pycountry

log = logging.getLogger(__name__)

# IOC codes that differ from the ISO alpha-3 code of the same country. Checked before ISO,
# because some IOC codes are another country's ISO code (IOC "BRN" Bahrain = ISO Brunei).
IOC_TO_ISO2 = {
    "AHO": "CW", "ALG": "DZ", "ANG": "AO", "ANT": "AG", "ARU": "AW", "ASA": "AS", "BAH": "BS",
    "BAN": "BD", "BAR": "BB", "BER": "BM", "BHU": "BT", "BIZ": "BZ", "BOT": "BW", "BRN": "BH",
    "BRU": "BN", "BUL": "BG", "BUR": "BF", "CAM": "KH", "CAY": "KY", "CGO": "CG", "CHA": "TD",
    "CHI": "CL", "CRC": "CR", "CRO": "HR", "DEN": "DK", "ESA": "SV", "FIJ": "FJ", "GAM": "GM",
    "GBS": "GW", "GEQ": "GQ", "GER": "DE", "GRE": "GR", "GRN": "GD", "GUA": "GT", "GUI": "GN",
    "HAI": "HT", "HON": "HN", "INA": "ID", "IRI": "IR", "ISV": "VI", "IVB": "VG", "KOS": "XK",
    "KSA": "SA", "KUW": "KW", "LAT": "LV", "LBA": "LY", "LES": "LS", "LIB": "LB", "MAD": "MG",
    "MAS": "MY", "MAW": "MW", "MGL": "MN", "MON": "MC", "MRI": "MU", "MTN": "MR", "MYA": "MM",
    "NCA": "NI", "NED": "NL", "NEP": "NP", "NGR": "NG", "NIG": "NE", "OMA": "OM", "PAR": "PY",
    "PHI": "PH", "PLE": "PS", "POR": "PT", "PUR": "PR", "RSA": "ZA", "SAM": "WS", "SEY": "SC",
    "SKN": "KN", "SLE": "SL", "SLO": "SI", "SOL": "SB", "SRI": "LK", "SUD": "SD", "SUI": "CH",
    "TAN": "TZ", "TGA": "TO", "TOG": "TG", "TPE": "TW", "UAE": "AE", "URU": "UY", "VAN": "VU",
    "VIE": "VN", "VIN": "VC", "ZAM": "ZM", "ZIM": "ZW",
}  # fmt: skip

# Names ESPN (or people) use that pycountry does not resolve, or resolves too broadly.
NAME_TO_ISO2 = {
    "usa": "US", "united states of america": "US", "england": "GB", "scotland": "GB",
    "wales": "GB", "northern ireland": "GB", "great britain": "GB", "russia": "RU",
    "south korea": "KR", "korea": "KR", "republic of korea": "KR", "north korea": "KP",
    "kosovo": "XK", "chinese taipei": "TW", "ivory coast": "CI", "dr congo": "CD",
    "macedonia": "MK", "turkey": "TR", "czech republic": "CZ", "palestine": "PS",
}  # fmt: skip

SHORT_NAMES = {  # (English, Korean) where pycountry's names are long or missing
    "KR": ("South Korea", "대한민국"), "KP": ("North Korea", "북한"), "US": ("USA", "미국"),
    "GB": ("United Kingdom", "영국"), "RU": ("Russia", "러시아"), "IR": ("Iran", "이란"),
    "TW": ("Taiwan", "대만"), "VN": ("Vietnam", "베트남"), "MD": ("Moldova", "몰도바"),
    "BO": ("Bolivia", "볼리비아"), "VE": ("Venezuela", "베네수엘라"), "SY": ("Syria", "시리아"),
    "TZ": ("Tanzania", "탄자니아"), "CZ": ("Czechia", "체코"), "XK": ("Kosovo", "코소보"),
    "CD": ("DR Congo", "콩고민주공화국"), "PS": ("Palestine", "팔레스타인"),
    "MK": ("North Macedonia", "북마케도니아"), "LA": ("Laos", "라오스"),
}  # fmt: skip


@cache
def to_iso2(value: str | None) -> str | None:
    """Map an IOC/ISO code or a country name to ISO alpha-2; None if unknown."""
    if not value or not value.strip():
        return None
    text = value.strip()
    upper = text.upper()
    if upper in IOC_TO_ISO2:
        return IOC_TO_ISO2[upper]
    if text.lower() in NAME_TO_ISO2:
        return NAME_TO_ISO2[text.lower()]
    try:
        return pycountry.countries.lookup(text).alpha_2
    except LookupError:
        log.warning("Unknown country %r", value)
        return None


@cache
def _korean() -> gettext.NullTranslations:
    try:
        return gettext.translation("iso3166-1", pycountry.LOCALES_DIR, languages=["ko"])
    except OSError:
        return gettext.NullTranslations()


def country_name(iso2: str, korean: bool = False) -> str:
    if iso2 in SHORT_NAMES:
        return SHORT_NAMES[iso2][1 if korean else 0]
    country = pycountry.countries.get(alpha_2=iso2)
    if country is None:
        return iso2
    if korean:
        return _korean().gettext(country.name)
    return getattr(country, "common_name", None) or country.name


def flag(iso2: str) -> str:
    """Regional-indicator flag emoji, e.g. "KR" -> 🇰🇷."""
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in iso2.upper())

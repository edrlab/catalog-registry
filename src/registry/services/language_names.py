"""English display names for BCP 47 language tags, from CLDR through Babel."""

from functools import cache
from typing import Final

from babel import Locale, UnknownLocaleError
from babel.core import get_global

#: CLDR's English names for every language it knows, including the many it has no locale data for
#: (`kha` Khasi, `chp` Chipewyan): `Locale.parse` fails on those, this table does not.
_CLDR_ENGLISH_NAMES: Final = Locale("en").languages

#: Official languages (`country_languages`) CLDR names nowhere, with their ISO 639-3 reference
#: names (SIL International, the ISO 639-3 registration authority).
ISO_639_3_NAMES: Final = {
    "bjt": "Balanta-Ganja",
    "bsc": "Bassari",
    "caa": "Ch'orti'",
    "cak": "Kaqchikel",
    "ixl": "Ixil",
    "knf": "Mankanya",
    "lbe": "Lak",
    "mdh": "Maguindanaon",
    "mey": "Hassaniyya",
    "mfv": "Mandjak",
    "quv": "Sakapulteko",
    "sav": "Saafi-Saafi",
    "snf": "Noon",
    "tnr": "Ménik",
    "tsg": "Tausug",
    "usp": "Uspanteco",
    "wni": "Ndzwani Comorian",
    "xin": "Xinca",
    "zdj": "Ngazidja Comorian",
}

#: Codes that name no language: the ISO 639-2 special codes and CLDR's root locale.
_SPECIAL_TAGS: Final = frozenset({"mis", "mul", "root", "und", "zxx"})


def build_language_name(tag: str) -> str:
    """ "fr" -> "French", "pt-br" -> "Portuguese (Brazil)", "kha" -> "Khasi", "tw" -> "Twi". A tag
    nobody names is its own name."""
    # CLDR's table first: `Locale.parse` follows aliases and would name "tw" (Twi) "Akan (Ghana)".
    if name := _CLDR_ENGLISH_NAMES.get(tag.lower()):
        return str(name)
    try:
        name = Locale.parse(tag, sep="-").get_display_name("en")
    except (UnknownLocaleError, ValueError):
        name = None
    if name:
        return str(name)
    base = tag.split("-", 1)[0].lower()
    return str(_CLDR_ENGLISH_NAMES.get(base) or ISO_639_3_NAMES.get(base) or tag)


@cache
def build_language_list() -> tuple[tuple[str, str], ...]:
    """Every language this module names, as (tag, name) sorted by name: CLDR's base language tags
    without the special codes or deprecated aliases (`tl` is listed once, as `fil`), plus
    `ISO_639_3_NAMES`. Built once: naming about 650 tags through Babel takes about 150 ms."""
    aliases = get_global("language_aliases")
    tags = {
        tag
        for tag in map(str, _CLDR_ENGLISH_NAMES)
        if "_" not in tag and tag not in aliases and tag not in _SPECIAL_TAGS
    } | ISO_639_3_NAMES.keys()
    named = ((tag, build_language_name(tag)) for tag in tags)
    return tuple(sorted(named, key=lambda item: item[1].casefold()))

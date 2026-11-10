"""English display names for BCP 47 language tags, from CLDR through Babel."""

from typing import Final

from babel import Locale, UnknownLocaleError

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

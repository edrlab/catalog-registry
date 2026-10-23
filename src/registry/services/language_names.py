"""English display names for BCP 47 language tags, from CLDR through Babel."""

from babel import Locale, UnknownLocaleError


def build_language_name(tag: str) -> str:
    """ "fr" -> "French", "pt-br" -> "Brazilian Portuguese". An unknown tag is its own name."""
    try:
        name = Locale.parse(tag, sep="-").get_display_name("en")
    except (UnknownLocaleError, ValueError):
        return tag
    return name or tag

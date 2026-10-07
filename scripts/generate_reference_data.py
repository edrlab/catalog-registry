"""Generate the v0.2 search reference data from pinned sources (ADR-057).

Writes CSV snapshots to `.cache/reference-data/out/` (gitignored, not part of the repo). The data
migration embeds the same rows as literals
(`--python` prints them), so regenerating the CSVs never changes a migration that already ran.
A CLDR update is a new data migration carrying the difference.

Sources, all pinned:
  CLDR 48.2, cldr-json tag 48.2.0:  territoryInfo.json (official languages),
                                    territories.json (country names)
  CLDR 48.2, cldr tag release-48-2: common/subdivisions/<lang>.xml (subdivision names)
  pycountry 26.2.16:                ISO 3166-2 subdivision types

pycountry is not a runtime dependency; run the script with
`make reference-data`.
Downloads are cached in `.cache/reference-data`.
"""

import argparse
import collections
import csv
import json
import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "migrations" / "versions"
#: Downloads are cached here, so a rerun is offline; the CSVs are written next to them (gitignored).
CACHE = REPO_ROOT / ".cache" / "reference-data"
OUT = CACHE / "out"
JSON_BASE = "https://raw.githubusercontent.com/unicode-org/cldr-json/48.2.0/cldr-json"
XML_BASE = "https://raw.githubusercontent.com/unicode-org/cldr/release-48-2/common/subdivisions"

HTTP_NOT_FOUND = 404

#: CLDR writes a trailing superscript digit on some names ("Île-de-France²").
SUPERSCRIPT = re.compile(r"[²³¹⁰-⁹]+$")

#: Everyday names CLDR lacks. Added by hand so a search for them finds the region.
HAND_SUBDIVISION_NAMES = [("BE-VLG", "fr", "Flandre"), ("BE-VLG", "nl", "Vlaanderen")]

#: French overseas subdivisions that are also CLDR territories (ADR-057). CLDR 48.2 has no
#: subdivision names for the letter-coded ones, and the territory names add everyday forms
#: ("French Guiana"), so both sets are used.
OVERSEAS_TERRITORY = {
    "FR-971": "GP",
    "FR-972": "MQ",
    "FR-973": "GF",
    "FR-974": "RE",
    "FR-976": "YT",
    "FR-BL": "BL",
    "FR-CP": "CP",
    "FR-MF": "MF",
    "FR-NC": "NC",
    "FR-PF": "PF",
    "FR-PM": "PM",
    "FR-TF": "TF",
    "FR-WF": "WF",
}


def download_cached(url: str, cache: Path) -> str:
    """Download once, then read from the cache, so a rerun is offline and reproducible.

    Raises `FileNotFoundError` for an HTTP 404 and remembers it, because CLDR genuinely has no
    file for some languages. Every other failure (network, 5xx) propagates: swallowing it would
    write incomplete CSVs without a word.
    """
    path = cache / re.sub(r"[^A-Za-z0-9.]+", "_", url[-90:])
    missing = path.with_name(path.name + ".404")
    if missing.exists():
        raise FileNotFoundError(url)
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                path.write_bytes(response.read())
        except urllib.error.HTTPError as error:
            if error.code != HTTP_NOT_FOUND:
                raise
            missing.touch()
            raise FileNotFoundError(url) from error
    return path.read_text(encoding="utf-8")


def extract_iso_countries() -> set[str]:
    """The 249 ISO 3166-1 codes the repo's `countries` table holds, from its initial migration."""
    text = next(MIGRATIONS.glob("c8e1b73f2d04_*.py")).read_text(encoding="utf-8")
    return set(re.findall(r'\("([A-Z]{2})", "[A-Z]{3}", "\d{3}"\)', text))


def extract_subdivision_codes() -> list[str]:
    """Subdivisions the repo seeds, across every migration that adds them."""
    codes: set[str] = set()
    for path in MIGRATIONS.glob("*.py"):
        codes.update(
            re.findall(r'\("([A-Z]{2}-[A-Z0-9]{1,3})", "[A-Z]{2}", None,', path.read_text())
        )
    return sorted(codes)


def generate_country_languages(iso: set[str], cache: Path) -> dict[str, tuple[str, list[str]]]:
    """Official languages; where a country has none, the de facto official ones.

    ISO 3166-1 codes only, script variants merged into the base language (`sr_Latn` -> `sr`),
    lowercase tags (ADR-053).
    """
    info = json.loads(
        download_cached(f"{JSON_BASE}/cldr-core/supplemental/territoryInfo.json", cache)
    )
    result: dict[str, tuple[str, list[str]]] = {}
    for country, data in info["supplemental"]["territoryInfo"].items():
        if country not in iso:
            continue
        population = data.get("languagePopulation", {})

        def pick(status: str, population: dict[str, dict[str, str]] = population) -> list[str]:
            return sorted(
                {
                    tag.split("_")[0].lower()
                    for tag, v in population.items()
                    if v.get("_officialStatus") == status
                }
            )

        official = pick("official")
        result[country] = (
            ("official", official) if official else ("de_facto_official", pick("de_facto_official"))
        )
    return result


def generate_country_names(
    iso: set[str], languages: dict[str, list[str]], cache: Path
) -> list[tuple[str, str, str]]:
    """English plus each country's official languages, with CLDR's short and variant forms."""
    wanted = sorted({"en"} | {tag for tags in languages.values() for tag in tags})
    rows: set[tuple[str, str, str]] = set()
    for lang in wanted:
        try:
            territories = download_territory_names(lang, cache)
        except FileNotFoundError:  # CLDR has no locale data for ~23 small languages
            continue
        for country in sorted(iso):
            if lang != "en" and lang not in languages.get(country, []):
                continue
            if country in territories:
                rows.add((country, lang, territories[country]))
            for alt in ("-alt-short", "-alt-variant"):
                name = territories.get(country + alt)
                if name and name != territories.get(country):
                    rows.add((country, lang, name))
    return sorted(rows)


def download_territory_names(lang: str, cache: Path) -> dict[str, str]:
    url = f"{JSON_BASE}/cldr-localenames-full/main/{lang}/territories.json"
    document = json.loads(download_cached(url, cache))
    territories: dict[str, str] = document["main"][lang]["localeDisplayNames"]["territories"]
    return territories


def generate_subdivision_names(
    codes: list[str], languages: dict[str, list[str]], cache: Path
) -> list[tuple[str, str, str]]:
    """Names for subdivisions present in the repo, in English plus the country's languages.

    CLDR writes subdivision codes as `bewal`; the repo uses `BE-WAL` (ADR-057).
    """
    rows: set[tuple[str, str, str]] = set(HAND_SUBDIVISION_NAMES)
    for code in codes:
        cldr_key = code.replace("-", "").lower()
        for lang in ["en", *languages[code[:2]]]:
            try:
                xml = download_cached(f"{XML_BASE}/{lang}.xml", cache)
            except FileNotFoundError:  # no subdivision file for this language
                continue
            pattern = rf'<subdivision type="{cldr_key}"( alt="[^"]+")?[^>]*>([^<]+)</subdivision>'
            for match in re.finditer(pattern, xml):
                rows.add((code, lang, SUPERSCRIPT.sub("", match.group(2)).strip()))
            if code in OVERSEAS_TERRITORY:
                name = download_territory_names(lang, cache).get(OVERSEAS_TERRITORY[code])
                if name:
                    rows.add((code, lang, name))
    return sorted(rows)


def generate_subdivision_types() -> list[tuple[str, str, int, int, str]]:
    """The ISO 3166-2 vocabulary: label, lowercase key, countries, subdivisions, country codes."""
    import pycountry  # noqa: PLC0415 - only this function needs it

    subdivisions = list(pycountry.subdivisions)
    count = collections.Counter(s.type for s in subdivisions)
    countries: dict[str, set[str]] = collections.defaultdict(set)
    for s in subdivisions:
        countries[s.type].add(s.country_code)
    ordered = sorted(count, key=lambda t: (-len(countries[t]), -count[t], t))
    return [
        (t, t.lower(), len(countries[t]), count[t], " ".join(sorted(countries[t]))) for t in ordered
    ]


def write_csv(path: Path, header: list[str], rows: Sequence[tuple[object, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--python", action="store_true", help="print the rows as Python literals")
    args = parser.parse_args()

    iso = extract_iso_countries()
    status_languages = generate_country_languages(iso, CACHE)
    languages = {country: tags for country, (_, tags) in status_languages.items()}
    language_rows = [
        (country, tag, status)
        for country, (status, tags) in sorted(status_languages.items())
        for tag in tags
    ]
    names = generate_country_names(iso, languages, CACHE)
    sub_names = generate_subdivision_names(extract_subdivision_codes(), languages, CACHE)
    types = generate_subdivision_types()

    write_csv(
        OUT / "country-languages-cldr48.csv",
        ["country_code", "language_tag", "cldr_status"],
        language_rows,
    )
    write_csv(OUT / "country-names-cldr48.csv", ["country_code", "language_tag", "name"], names)
    write_csv(
        OUT / "subdivision-names-cldr48.csv",
        ["subdivision_code", "language_tag", "name"],
        sub_names,
    )
    write_csv(
        OUT / "iso-3166-2-subdivision-types.csv",
        ["iso_label", "subdivision_type", "countries", "subdivisions", "country_codes"],
        types,
    )
    print(
        f"country_languages {len(language_rows)}, country_names {len(names)}, "
        f"subdivision_names {len(sub_names)}, subdivision types {len(types)}"
    )

    if args.python:
        for name, rows in (
            ("COUNTRY_LANGUAGES", [(c, t) for c, t, _ in language_rows]),
            ("COUNTRY_NAMES", names),
            ("SUBDIVISION_NAMES", sub_names),
        ):
            print(f"{name} = [")
            for row in rows:
                print(f"    {row!r},")
            print("]")


if __name__ == "__main__":
    main()

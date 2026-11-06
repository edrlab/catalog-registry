"""Generate the v0.2 search reference data from pinned sources.

Writes CSV snapshots to `.cache/reference-data/out/` (gitignored, not part of the repo). The data
migration embeds the same rows as literals
(`--python` prints them), so regenerating the CSVs never changes a migration that already ran.
A CLDR update is a new data migration carrying the difference.

Sources, all pinned:
  CLDR 49, cldr-json tag 49.0.0:    territoryInfo.json (official languages),
                                    territories.json (country names)
  CLDR 49, cldr tag release-49:     common/subdivisions/<lang>.xml (subdivision names)
  pycountry 26.2.16:                ISO 3166-2 subdivision types

pycountry is not a runtime dependency; run the script with
`make reference-data`.
Downloads are cached in `.cache/reference-data`.
"""

import argparse
import ast
import collections
import csv
import html
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
#: The one place the CLDR release is pinned. `scripts/check_reference_sources.py` compares it with
#: the latest published release; a bump is a new data migration carrying the difference.
CLDR_VERSION = "49.0.0"


def build_cldr_xml_tag(version: str) -> str:
    """The cldr repository's tag for a cldr-json version: `48.2.0` → `release-48-2`,
    `49.0.0` → `release-49`."""
    major, minor, patch = version.split(".")
    return "-".join(
        [
            "release",
            major,
            *([minor] if minor != "0" or patch != "0" else []),
            *([patch] if patch != "0" else []),
        ]
    )


JSON_BASE = f"https://raw.githubusercontent.com/unicode-org/cldr-json/{CLDR_VERSION}/cldr-json"
XML_BASE = (
    "https://raw.githubusercontent.com/unicode-org/cldr/"
    f"{build_cldr_xml_tag(CLDR_VERSION)}/common/subdivisions"
)

#: Every CLDR status that makes a language official somewhere in a country, strongest first.
#: Counting only the country-wide `official` status gave India 2 languages of 22, South Africa 1 of
#: 11 and Spain no Catalan, Basque or Galician, so the regional, minority and de facto statuses
#: count too.
OFFICIAL_STATUSES = ("official", "de_facto_official", "official_regional", "official_minority")

#: Official languages CLDR does not list as official for the country. Each with its source.
HAND_COUNTRY_LANGUAGES = [
    # Constitution of India, Eighth Schedule (Bodo and Dogri: 92nd Amendment, 2003; Manipuri:
    # 71st Amendment, 1992). CLDR 48.2 gives these three no official status in India.
    ("IN", "brx", "official_regional"),
    ("IN", "doi", "official_regional"),
    ("IN", "mni", "official_regional"),
]

HTTP_NOT_FOUND = 404

#: CLDR writes a trailing superscript digit on some names ("Île-de-France²").
SUPERSCRIPT = re.compile(r"[²³¹⁰-⁹]+$")

#: Everyday names CLDR lacks. Added by hand so a search for them finds the region.
HAND_SUBDIVISION_NAMES = [("BE-VLG", "fr", "Flandre"), ("BE-VLG", "nl", "Vlaanderen")]

#: French overseas subdivisions that are also CLDR territories. CLDR (48.2, 49) has no
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


def clean_cldr_name(raw: str) -> str:
    """A name as CLDR's XML writes it, made plain: entities decoded (`&amp;` is `&`; the files are
    read with a regex, not an XML parser) and the trailing footnote digit dropped."""
    return SUPERSCRIPT.sub("", html.unescape(raw)).strip()


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


def generate_country_languages(iso: set[str], cache: Path) -> dict[str, list[tuple[str, str]]]:
    """Every language official anywhere in each country: (tag, CLDR status), strongest status per
    tag, plus `HAND_COUNTRY_LANGUAGES`.

    ISO 3166-1 codes only, script variants merged into the base language (`sr_Latn` -> `sr`),
    lowercase tags (BCP-47 writes the language subtag lowercase).
    """
    info = json.loads(
        download_cached(f"{JSON_BASE}/cldr-core/supplemental/territoryInfo.json", cache)
    )
    found: dict[str, dict[str, str]] = collections.defaultdict(dict)
    entries = [
        (country, tag.split("_")[0].lower(), data.get("_officialStatus"))
        for country, territory in info["supplemental"]["territoryInfo"].items()
        for tag, data in territory.get("languagePopulation", {}).items()
    ]
    entries += HAND_COUNTRY_LANGUAGES
    for country, tag, status in entries:
        if country not in iso or status not in OFFICIAL_STATUSES:
            continue
        known = found[country].get(tag)
        if known is None or OFFICIAL_STATUSES.index(status) < OFFICIAL_STATUSES.index(known):
            found[country][tag] = status
    return {country: sorted(tags.items()) for country, tags in found.items()}


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

    CLDR writes subdivision codes as `bewal`; the repo uses ISO 3166-2's `BE-WAL`.
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
                rows.add((code, lang, clean_cldr_name(match.group(2))))
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


def generate_all_subdivisions(iso: set[str]) -> list[tuple[str, str, str | None, str]]:
    """Every ISO 3166-2 subdivision pycountry knows for a country in `countries`:
    (code, country, parent code, type key), parents before children so a plain INSERT order
    satisfies `subdivisions.parent_code`'s foreign key."""
    import pycountry  # noqa: PLC0415 - only the generator needs it

    known = {s.code: s for s in pycountry.subdivisions if s.country_code in iso}

    def depth(code: str) -> int:
        parent = known[code].parent_code
        return 0 if not parent or parent not in known else 1 + depth(parent)

    return sorted(
        (
            (
                code,
                s.country_code,
                s.parent_code if s.parent_code in known else None,
                s.type.lower(),
            )
            for code, s in known.items()
        ),
        key=lambda row: (depth(row[0]), row[0]),
    )


def _read_cldr_subdivisions(lang: str, cache: Path) -> dict[str, list[str]]:
    """One CLDR subdivisions file as {cldr key: names}, alternates included. Empty if CLDR has
    no subdivision names for *lang*."""
    try:
        xml = download_cached(f"{XML_BASE}/{lang}.xml", cache)
    except FileNotFoundError:
        return {}
    names: dict[str, list[str]] = collections.defaultdict(list)
    for match in re.finditer(
        r'<subdivision type="([a-z0-9]+)"( alt="[^"]+")?[^>]*>([^<]+)</subdivision>', xml
    ):
        names[match.group(1)].append(clean_cldr_name(match.group(3)))
    return names


def generate_all_subdivision_names(
    subdivisions: Sequence[tuple[str, str, str | None, str]],
    languages: dict[str, list[str]],
    cache: Path,
) -> list[tuple[str, str, str]]:
    """Names for every subdivision: English plus its country's official languages from CLDR, the
    overseas territory names, the hand-added ones, and pycountry's English name wherever CLDR has
    no English name, so every subdivision can be shown and searched."""
    import pycountry  # noqa: PLC0415

    english = {s.code: s.name for s in pycountry.subdivisions}
    files: dict[str, dict[str, list[str]]] = {}
    rows: set[tuple[str, str, str]] = set(HAND_SUBDIVISION_NAMES)
    for code, country, _parent, _type in subdivisions:
        key = code.replace("-", "").lower()
        for lang in ["en", *languages.get(country, [])]:
            if lang not in files:
                files[lang] = _read_cldr_subdivisions(lang, cache)
            rows.update((code, lang, name) for name in files[lang].get(key, []))
            if code in OVERSEAS_TERRITORY:
                name = download_territory_names(lang, cache).get(OVERSEAS_TERRITORY[code])
                if name:
                    rows.add((code, lang, name))
        if not any(row[0] == code and row[1] == "en" for row in rows):
            rows.add((code, "en", english[code]))
    return sorted(rows)


#: The columns of each table an `--update` writes, in the migrations' order.
UPDATE_COLUMNS = {
    "country_languages": ("country_code", "language_tag"),
    "country_names": ("country_code", "language_tag", "name"),
    "subdivision_names": ("subdivision_code", "language_tag", "name"),
}
_LITERAL_NAMES = {
    "COUNTRY_LANGUAGES": "country_languages",
    "COUNTRY_NAMES": "country_names",
    "SUBDIVISION_NAMES": "subdivision_names",
}


def extract_loaded_rows() -> dict[str, set[tuple[str, ...]]]:
    """Every reference row an existing migration already inserts: the literals of the earlier data
    migrations and the CSV files under `migrations/data/`. An update writes only what is missing."""
    loaded: dict[str, set[tuple[str, ...]]] = {table: set() for table in UPDATE_COLUMNS}
    for path in MIGRATIONS.glob("*.py"):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                table = _LITERAL_NAMES.get(node.targets[0].id)
                if table:
                    loaded[table] |= {tuple(row) for row in ast.literal_eval(node.value)}
    for table in UPDATE_COLUMNS:
        for csv_path in (MIGRATIONS.parent / "data").glob(f"*/{table}.csv"):
            with csv_path.open(encoding="utf-8", newline="") as handle:
                loaded[table] |= {tuple(row) for row in list(csv.reader(handle))[1:]}
    return loaded


def write_csv(path: Path, header: list[str], rows: Sequence[tuple[object, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--python", action="store_true", help="print the rows as Python literals")
    parser.add_argument(
        "--update",
        type=Path,
        metavar="DIR",
        help="write only the rows no migration holds yet (languages, country names, subdivision "
        "names) as CSV files into DIR: the data of the next reference-data migration",
    )
    parser.add_argument(
        "--full-subdivisions",
        type=Path,
        metavar="DIR",
        help="also write every ISO 3166-2 subdivision and its names as CSV files into DIR "
        "(the full-subdivisions data migration's data)",
    )
    args = parser.parse_args()

    iso = extract_iso_countries()
    status_languages = generate_country_languages(iso, CACHE)
    languages = {country: [tag for tag, _ in tags] for country, tags in status_languages.items()}
    language_rows = [
        (country, tag, status)
        for country, tags in sorted(status_languages.items())
        for tag, status in tags
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

    if args.full_subdivisions:
        every = generate_all_subdivisions(iso)
        every_names = generate_all_subdivision_names(every, languages, CACHE)
        write_csv(
            args.full_subdivisions / "subdivisions.csv",
            ["code", "country_alpha2", "parent_code", "subdivision_type"],
            [(code, country, parent or "", kind) for code, country, parent, kind in every],
        )
        write_csv(
            args.full_subdivisions / "subdivision_names.csv",
            ["subdivision_code", "language_tag", "name"],
            every_names,
        )
        print(f"all subdivisions {len(every)}, their names {len(every_names)}")

    if args.update:
        every_names = generate_all_subdivision_names(
            generate_all_subdivisions(iso), languages, CACHE
        )
        loaded = extract_loaded_rows()
        for table, rows in (
            ("country_languages", [(c, t) for c, t, _ in language_rows]),
            ("country_names", names),
            ("subdivision_names", every_names),
        ):
            new_rows = sorted(set(rows) - loaded[table])
            write_csv(args.update / f"{table}.csv", list(UPDATE_COLUMNS[table]), new_rows)
            print(f"{table}: {len(new_rows)} new")

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

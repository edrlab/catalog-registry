"""`python -m registry.cli seed` argument parsing. Pure, no database."""

from pathlib import Path

import pytest

from registry.cli.seed import build_seed_parser
from registry.cli.seed import main as seed_main
from registry.core.errors import ValidationError

pytestmark = pytest.mark.unit


def test_no_arguments_leaves_the_file_to_settings() -> None:
    """`make seed` passes nothing, and `REGISTRY_SEED_FILE` still decides."""
    arguments = build_seed_parser().parse_args([])

    assert arguments.file is None
    assert arguments.recommended is True


def test_a_named_file_is_read_as_a_path() -> None:
    arguments = build_seed_parser().parse_args(["data/libraries.json"])

    assert arguments.file == Path("data/libraries.json")
    assert arguments.recommended is True


def test_no_recommended_clears_the_flag() -> None:
    """The failure this guards: a `store_true` here would recommend the libraries silently."""
    arguments = build_seed_parser().parse_args(["data/libraries.json", "--no-recommended"])

    assert arguments.file == Path("data/libraries.json")
    assert arguments.recommended is False


def test_an_unknown_flag_is_rejected() -> None:
    """argparse exits 2 rather than seeding something the caller did not ask for."""
    with pytest.raises(SystemExit) as exit_info:
        build_seed_parser().parse_args(["--recomended"])

    assert exit_info.value.code == 2


def test_no_recommended_requires_a_file() -> None:
    """Without a file it would target `REGISTRY_SEED_FILE` and un-recommend the whole feed.

    `import_catalog_document` writes `recommended` unconditionally, so one command would flip
    all of `data/recommended.json` out of `GET /` — the inverse of the mistake this flag exists
    to prevent, and it contradicts `seed_catalogs`' promise that unrecommending is manual.
    """
    with pytest.raises(SystemExit) as exit_info:
        seed_main(["--no-recommended"])

    assert exit_info.value.code == 2


def test_a_missing_file_is_a_readable_message_not_a_database_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`FileNotFoundError` is an `OSError`, and `cli/__main__.py` reports those as an unreachable
    database — so a mistyped path used to print "Is it running? make up" and never name the file.
    """
    missing = tmp_path / "not-here.json"

    with pytest.raises(ValidationError, match=str(missing)):
        seed_main([str(missing)])

    assert capsys.readouterr().out == ""

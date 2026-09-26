"""`python -m registry.cli seed` argument parsing. Pure, no database."""

from pathlib import Path

import pytest

from registry.cli.seed import build_seed_parser

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

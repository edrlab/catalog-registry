"""`python -m registry.cli <command> [arguments]`."""

import sys
from collections.abc import Callable, Sequence

from registry.cli.add import main as add_main
from registry.cli.seed import main as seed_main

COMMANDS: dict[str, Callable[[Sequence[str]], int]] = {"seed": seed_main, "add": add_main}


def main() -> int:
    match sys.argv[1:]:
        case [command, *arguments] if command in COMMANDS:
            return COMMANDS[command](arguments)
        case _:
            print(f"usage: python -m registry.cli {{{'|'.join(COMMANDS)}}} [arguments]")
            return 2


if __name__ == "__main__":
    sys.exit(main())

"""`python -m registry.cli <command> [arguments]`."""

import sys
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from registry.cli.add import main as add_main
from registry.cli.seed import main as seed_main
from registry.core.config import Settings
from registry.core.errors import RegistryError

COMMANDS: dict[str, Callable[[Sequence[str]], int]] = {"seed": seed_main, "add": add_main}


def report_unreachable_database(error: OSError) -> int:
    """Turn a connection failure into an answer rather than a traceback.

    Every CLI command is a host process reaching Postgres through its published port, so the
    common failure is not a defect: the stack is down, or `.env` names a port that compose is
    not publishing. A stack trace says none of that.
    """
    dsn = urlsplit(str(Settings().database_url))
    print(f"Cannot reach the database at {dsn.hostname}:{dsn.port}.", file=sys.stderr)
    print(file=sys.stderr)
    print("  Is it running?          make up", file=sys.stderr)
    print(
        "  Started on another port? REGISTRY_DATABASE_URL in .env must name the same",
        file=sys.stderr,
    )
    print("                           port as DB_PORT, for example:", file=sys.stderr)
    print("                             make up DB_PORT=55432", file=sys.stderr)
    print(f"\n({error})", file=sys.stderr)
    return 1


def main() -> int:
    match sys.argv[1:]:
        case [command, *arguments] if command in COMMANDS:
            try:
                return COMMANDS[command](arguments)
            except RegistryError as error:
                # Deliberately raised, with a message written to be read. A traceback here
                # would bury it.
                print(error, file=sys.stderr)
                return 1
            except OSError as error:
                return report_unreachable_database(error)
        case _:
            print(f"usage: python -m registry.cli {{{'|'.join(COMMANDS)}}} [arguments]")
            return 2


if __name__ == "__main__":
    sys.exit(main())

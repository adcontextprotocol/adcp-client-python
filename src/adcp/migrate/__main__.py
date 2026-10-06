"""CLI dispatcher: ``python -m adcp.migrate <migration> <path>``.

Usage::

    python -m adcp.migrate v3-to-v4 ./src
    python -m adcp.migrate v3-to-v4 ./src --apply
    python -m adcp.migrate v3-to-v4 ./src --json

Each migration owns its own argparse surface (see e.g.
:func:`adcp.migrate.v3_to_v4.main`). This dispatcher just routes the
first positional argument.
"""

from __future__ import annotations

import sys


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help"}:
        print(
            "usage: python -m adcp.migrate <migration> <path> [options]\n"
            "\n"
            "Migrations:\n"
            "  v3-to-v4    Rewrite <Type>Asset → <Type>Content, rename\n"
            "              adcp.types.generated_poc imports to adcp.types.domains,\n"
            "              and flag removed-type usages (BrandManifest, DeliverTo, etc).\n"
            "\n"
            "Run `python -m adcp.migrate <migration> --help` for per-migration options.\n",
            file=sys.stderr,
        )
        return 0 if argv and argv[0] in {"-h", "--help"} else 2

    migration = argv[0]
    if migration == "v3-to-v4":
        from adcp.migrate.v3_to_v4 import main as migrate_main

        return migrate_main(argv[1:])

    print(f"error: unknown migration {migration!r}", file=sys.stderr)
    print("Known: v3-to-v4", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

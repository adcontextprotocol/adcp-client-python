"""Correct two request-fixture bugs in the published SDK 14 test bundle.

SDK 14's 3.2.1 get_products_rejected scenario still sends 3.2-beta.6.
Use release-precision wire value 3.2 for that scenario without otherwise
changing requests, validations, or the server's supported versions.
invalid_transitions creates a sandbox buy through runner enrichment but its
negative probes default to the live account. Explicit sandbox references keep
the entire chain in the same account without relaxing ownership checks.
Remove this workaround when upgrading the pinned runner past 14.0.0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def prepare_sdk(root: Path) -> None:
    if json.loads((root / "package.json").read_text())["version"] != "14.0.0":
        return
    for tree in ("domains", "protocols"):
        scenario = (
            root
            / "compliance/cache/3.2.1"
            / tree
            / "media-buy/scenarios/get_products_rejected.yaml"
        )
        original = scenario.read_text()
        stale = 'adcp_version: "3.2-beta.6"'
        current = 'adcp_version: "3.2"'
        count = original.count(stale)
        if count == 0 and original.count(current) == 2:
            pass  # Idempotent preparation of a shared local SDK install.
        elif count != 2:
            raise ValueError(f"Unexpected SDK 14 fixture contents: {scenario}")
        else:
            scenario.write_text(original.replace(stale, current))
            print(f"Corrected SDK 14 stale prerelease requests in {scenario}")

        scenario = scenario.with_name("invalid_transitions.yaml")
        original = scenario.read_text()
        account_unit = (
            '              id: "compliance-media_buy_seller_invalid_transitions-adb17ad2"\n'
        )
        sandbox_unit = account_unit + "            sandbox: true\n"
        if original.count(account_unit) != 6:
            raise ValueError(f"Unexpected SDK 14 account fixture contents: {scenario}")
        if original.count(sandbox_unit) == 6:
            continue
        if sandbox_unit in original:
            raise ValueError(f"Partially corrected SDK 14 account fixture: {scenario}")
        scenario.write_text(original.replace(account_unit, sandbox_unit))
        print(f"Kept SDK 14 negative-path requests in their sandbox account: {scenario}")


if __name__ == "__main__":
    prepare_sdk(Path(sys.argv[1]))

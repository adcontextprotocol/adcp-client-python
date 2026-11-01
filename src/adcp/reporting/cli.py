"""``python -m adcp reporting``: explain coverage and compare traces (AdCP 3.3, experimental).

Seller side, against the seller's own service and storage::

    python -m adcp reporting explain --app myapp.reporting:service \\
        --principal agency-x --account acc_renault_fr --period 2026-11-03 [--json]

``--app`` names the seller's :class:`~adcp.reporting.service.ReliableReportingService`
as ``module:attribute`` (a callable attribute is called, and awaited if it is
a coroutine function), the way ASGI servers name an app. ``--tenant`` sets the
principal's tenant.

Comparing a buyer's trace with a seller's::

    python -m adcp reporting diff buyer.json seller.json

prints the first rule whose facts differ, and whether the two sides read the
same inputs (equal ``inputs_digest``: a logic bug) or different state.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import sys
from collections.abc import Sequence
from typing import Any

USAGE = "python -m adcp reporting {explain,diff} ..."


def _load_app(target: str) -> Any:
    module_name, _, attribute = target.partition(":")
    if not module_name or not attribute:
        raise SystemExit("--app is module:attribute, for example myapp.reporting:service")
    module = importlib.import_module(module_name)
    value: Any = module
    for part in attribute.split("."):
        value = getattr(value, part)
    return value


async def _resolve_app(target: str) -> Any:
    app = _load_app(target)
    if callable(app) and not hasattr(app, "explain_coverage"):
        app = app()
    if inspect.isawaitable(app):
        app = await app
    if not hasattr(app, "explain_coverage"):
        raise SystemExit(f"{target} is not a ReliableReportingService")
    return app


def _print_traces(traces: Sequence[Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps([trace.to_dict() for trace in traces], indent=2))
        return
    if not traces:
        print("No period matches: check the account, the subscription and the period.")
        return
    for trace in traces:
        subject = trace.subject
        period = subject["period"]
        print(
            f"{subject['subscription_id']} v{subject['subscription_version']} "
            f"{subject.get('media_buy_id') or '-'} {subject.get('offering_id') or '-'} "
            f"{period['start']}..{period['end']} ({period['timezone']}): "
            f"{trace.verdict} [{trace.reason}]"
        )
        print(f"  {trace.explanation}")


async def _explain(args: argparse.Namespace) -> int:
    from adcp._principal_ref import PrincipalRef

    service = await _resolve_app(args.app)
    traces = await service.explain_coverage(
        PrincipalRef(args.tenant, args.principal),
        args.account,
        period=args.period,
        subscription_id=args.subscription,
        media_buy_id=args.media_buy,
        offering_id=args.offering,
    )
    _print_traces(traces, as_json=args.json)
    return 0


def _read_traces(path: str) -> list[Any]:
    from adcp.reporting.subscriptions.explain import CoverageTrace

    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    items = document if isinstance(document, list) else [document]
    return [CoverageTrace.from_dict(item) for item in items]


def _diff(args: argparse.Namespace) -> int:
    from adcp.reporting.subscriptions.explain import diff_coverage_traces

    left, right = _read_traces(args.left), _read_traces(args.right)

    def key(trace: Any) -> tuple[Any, ...]:
        subject = trace.subject
        return (
            subject["account_id"],
            subject["subscription_id"],
            subject.get("media_buy_id"),
            subject.get("offering_id"),
            subject["period"]["start"],
            subject["period"]["end"],
        )

    rights = {key(trace): trace for trace in right}
    differences = 0
    for trace in left:
        other = rights.pop(key(trace), None)
        if other is None:
            print(f"Only in {args.left}: {key(trace)}")
            differences += 1
            continue
        found = diff_coverage_traces(trace, other)
        if found is not None:
            print(f"{key(trace)}: {found.describe()}")
            differences += 1
    for missing in rights:
        print(f"Only in {args.right}: {missing}")
        differences += 1
    if not differences:
        print("The traces agree.")
    return 1 if differences else 0


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m adcp reporting", usage=USAGE)
    commands = parser.add_subparsers(dest="command", required=True)
    explain = commands.add_parser(
        "explain", help="Explain why periods are or are not owed (seller, from your service)"
    )
    explain.add_argument("--app", required=True, help="module:attribute of your service")
    explain.add_argument("--principal", required=True, help="principal id")
    explain.add_argument("--tenant", default=None, help="tenant id, when multi-tenant")
    explain.add_argument("--account", required=True, help="account_id")
    explain.add_argument(
        "--period",
        required=True,
        help="YYYY-MM-DD read in each offering's zone, or an RFC 3339 instant",
    )
    explain.add_argument("--subscription", default=None, help="one subscription_id")
    explain.add_argument("--media-buy", default=None, help="one media_buy_id")
    explain.add_argument("--offering", default=None, help="one offering_id")
    explain.add_argument("--json", action="store_true", help="print the CoverageTrace JSON")
    diff = commands.add_parser("diff", help="Compare two CoverageTrace JSON files")
    diff.add_argument("left")
    diff.add_argument("right")
    args = parser.parse_args(list(argv))
    if args.command == "diff":
        return _diff(args)
    return asyncio.run(_explain(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))

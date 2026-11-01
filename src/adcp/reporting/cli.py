"""``python -m adcp reporting``: explain coverage and compare traces (AdCP 3.3, experimental).

Seller side, against the seller's own service and storage::

    python -m adcp reporting explain --app myapp.reporting:service \\
        --principal agency-x --account acc_renault_fr --period 2026-11-03 [--json]

``--app`` names the seller's :class:`~adcp.reporting.service.ReliableReportingService`
as ``module:attribute`` (a callable attribute is called, and awaited if it is
a coroutine function), the way ASGI servers name an app. ``--tenant`` sets the
principal's tenant.

Buyer side, against a saved agent alias, a URL or a JSON agent config::

    python -m adcp reporting explain --agent my-seller --account acc_a --period 2026-11-03
    python -m adcp reporting reconcile --agent my-seller [--since 2026-11-01] [--json]

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

USAGE = "python -m adcp reporting {explain,reconcile,diff} ..."


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


def _agent_client(identifier: str) -> Any:
    from adcp.__main__ import resolve_agent_config
    from adcp.client import ADCPClient
    from adcp.types.core import AgentConfig, Protocol

    config = dict(resolve_agent_config(identifier))
    config.setdefault("id", config.get("agent_uri", "unknown"))
    protocol = config.get("protocol", "mcp")
    config["protocol"] = Protocol(protocol.lower()) if isinstance(protocol, str) else protocol
    return ADCPClient(AgentConfig(**config))


async def _explain(args: argparse.Namespace) -> int:
    from adcp._principal_ref import PrincipalRef

    if args.agent:
        async with _agent_client(args.agent) as client:
            traces = await client.reporting.explain(
                account_id=args.account,
                period=args.period,
                subscription_id=args.subscription,
                media_buy_id=args.media_buy,
                offering_id=args.offering,
            )
        _print_traces(traces, as_json=args.json)
        return 0
    if not args.principal:
        raise SystemExit("--app needs --principal")
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


async def _reconcile(args: argparse.Namespace) -> int:
    async with _agent_client(args.agent) as client:
        report = await client.reporting.reconcile(
            since=args.since, include_not_owed=args.include_not_owed
        )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    summary = report.summary
    print(
        f"{'definitive' if report.definitive else 'NOT definitive'} as of {report.as_of}: "
        f"{summary.owed} owed, {summary.delivered} delivered, {summary.pending} pending, "
        f"{summary.late} late, {summary.missing_obligation} missing, "
        f"{summary.withheld} withheld"
    )
    for issue in report.issues:
        print(f"  ! {issue.code}: {issue.message}")
    for line in report.periods:
        print(
            f"{line.account_id} {line.subscription_id} v{line.subscription_version} "
            f"{line.period['start']}..{line.period['end']} {line.status} [{line.reason}] "
            f"{line.responsible_party}"
        )
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
    source = explain.add_mutually_exclusive_group(required=True)
    source.add_argument("--app", help="seller: module:attribute of your service")
    source.add_argument("--agent", help="buyer: saved alias, URL or JSON agent config")
    explain.add_argument("--principal", default=None, help="seller: principal id")
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
    reconcile = commands.add_parser(
        "reconcile", help="List what a seller owes your subscriptions, and each status (buyer)"
    )
    reconcile.add_argument("--agent", required=True, help="saved alias, URL or JSON config")
    reconcile.add_argument("--since", default=None, help="skip periods starting earlier")
    reconcile.add_argument("--include-not-owed", action="store_true")
    reconcile.add_argument("--json", action="store_true")
    diff = commands.add_parser("diff", help="Compare two CoverageTrace JSON files")
    diff.add_argument("left")
    diff.add_argument("right")
    args = parser.parse_args(list(argv))
    if args.command == "diff":
        return _diff(args)
    if args.command == "reconcile":
        return asyncio.run(_reconcile(args))
    return asyncio.run(_explain(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))

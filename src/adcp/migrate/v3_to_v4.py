"""v3 → v4 migration for the AdCP SDK.

The spec redesign in 4.0 renamed the 9 ``<Type>Asset`` payload
classes to ``<Type>Content`` and removed several legacy types
(``BrandManifest``, ``DeliverTo``, ``Pricing``, ``PromotedProducts``,
``PromotedOfferings``, ``FormatCategory``, ``PackageStatus``). This
module does the mechanical rewrites and prints a structured report of
everything that still needs human attention.

Two kinds of findings:

* **Applied**: direct name rewrites (``AudioAsset`` → ``AudioContent``
  etc). The 9 rename targets are distinctive enough that word-boundary
  regex is safe; sellers should still review the diff. The
  ``adcp.types.generated_poc`` → ``adcp.types.domains`` import-path
  rename is applied the same way (#1417): the generated tree moved
  there with its module stems unchanged, and the old path returns the
  same objects, so the rewrite is mechanical. The one stem that split,
  ``brand``, is resolved per imported name against the two halves it
  became (``domains.brand_discovery`` for ``brand.json``'s classes,
  ``domains.brand`` for the task-schema aggregator) and flagged when
  it cannot be.
* **Flagged**: removed types and numbered ``Assets<N>`` imports.
  These don't rewrite — the seller has to choose the replacement
  (e.g. ``BrandManifest`` → ``BrandReference(domain=...)`` depends on
  call-site context).

Invocation::

    python -m adcp.migrate v3-to-v4 ./src               # dry run, report only
    python -m adcp.migrate v3-to-v4 ./src --apply       # rewrite files in place
    python -m adcp.migrate v3-to-v4 ./src --auto-apply  # also rewrite safe imports
    python -m adcp.migrate v3-to-v4 ./src --json        # structured report

The dry run is the default — you always see what would change before
anything moves. ``--apply`` rewrites the 9 ``<Type>Asset`` renames and
the ``generated_poc`` → ``domains`` import paths in place.
``--auto-apply`` implies ``--apply`` and additionally lifts a
``generated_poc`` import to ``adcp.types`` when every name on the line is
a known public alias there (the flat surface is the first choice; a
domain path is for a name it cannot bind), and rewrites ``flag_numbered``
findings with a documented semantic alias (``Assets81`` →
``VideoFormatAsset``, etc.).  ``flag_removed`` findings always require
human review and remain flagged even with ``--auto-apply``.  Commit your
tree before running either write mode so ``git diff`` is your review view.

.. important::
   The codemod matches identifiers textually (word-boundary regex, not
   AST). That's deliberate — attribute accesses, imports, type
   annotations, and f-string-interpolated type names all need the
   rename, and a text-match catches every context a caller cares
   about. The tradeoff: a string literal like
   ``ERROR_MSG = "AudioAsset deprecated"`` or a comment mentioning
   ``AudioAsset`` will rewrite. Review the ``git diff`` for these
   cases (usually trivially reverted) — they are the one class of
   false positive the regex approach produces.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# The 9 spec rename mappings — payload ``<Type>Asset`` → ``<Type>Content``.
# Order matters only for predictable report output; the regex replaces
# each name independently.
ASSET_CONTENT_RENAMES: dict[str, str] = {
    "AudioAsset": "AudioContent",
    "CssAsset": "CssContent",
    "HtmlAsset": "HtmlContent",
    "ImageAsset": "ImageContent",
    "JavascriptAsset": "JavascriptContent",
    "TextAsset": "TextContent",
    "UrlAsset": "UrlContent",
    "VideoAsset": "VideoContent",
    "WebhookAsset": "WebhookContent",
}


# Removed types — no auto-replacement possible, flag with migration hint.
# Paired with an anchor slug in MIGRATION_v3_to_v4.md so operators can
# jump straight to the replacement pattern.
REMOVED_TYPES: dict[str, tuple[str, str]] = {
    "BrandManifest": (
        "use BrandReference(domain=...) on requests; read ResolvedBrand.brand from the registry",
        "brandmanifest--brandreference",
    ),
    "FormatCategory": (
        "removed — format category info lives on Format metadata",
        "formatcategory--removed",
    ),
    "DeliverTo": (
        "use publisher_properties on the request",
        "deliverto--publisher_properties",
    ),
    "PromotedProducts": (
        "use the spec-current offerings shape",
        "promotedproducts--promotedofferings--offerings",
    ),
    "PromotedOfferings": (
        "use the spec-current offerings shape",
        "promotedproducts--promotedofferings--offerings",
    ),
    "Pricing": (
        "use the discriminated *PricingOption classes (e.g. CpmFixedRatePricingOption)",
        "pricing--discriminated-pricingoption",
    ),
    "PackageStatus": (
        "package status is now carried by MediaBuyStatus",
        "packagestatus--mediabuystatus",
    ),
}


# Attribute accesses that moved / were removed. Flagged not rewritten
# because context determines the right replacement.
REMOVED_ATTRIBUTE_ACCESSES: dict[str, str] = {
    ".brand_manifest": ("ResolvedBrand.brand_manifest removed — use .brand instead"),
}


# Enum values removed or split between v3 and v4. Flagged (not rewritten)
# because the correct replacement depends on call-site semantics.
REMOVED_ENUM_VALUES: dict[str, tuple[str, str]] = {
    "MediaBuyStatus.pending_activation": (
        "`pending_activation` split in v4: use `pending_start` if the buy hasn't reached "
        "its scheduled start date, or `pending_creatives` if creatives haven't been "
        "submitted. Check `valid_actions` on the MediaBuy response to confirm which applies.",
        "mediabuystatuspending_activation--split",
    ),
}


# The pre-9.0 generated tree's import path and where it moved (#1417). Every
# module stem under the first is served by the same stem under the second, so
# the rewrite is a prefix rename, with one exception: a root discovery schema
# whose basename collided with a task-schema directory split into
# ``<stem>_discovery`` (its own classes) and ``<stem>`` (the aggregator).
# ``adcp.types._generated_poc_alias`` is the runtime half of the same contract.
DEPRECATED_TYPES_ROOT = "adcp.types.generated_poc"
CANONICAL_TYPES_ROOT = "adcp.types.domains"
ROOT_DISCOVERY_SUFFIX = "_discovery"

# Any ``adcp.types.generated_poc[.a.b]`` reference — an ``import`` statement,
# a dotted attribute access, the module half of a multi-line ``from`` import.
_GENERATED_POC_REF = re.compile(r"\badcp\.types\.generated_poc(?P<rest>(?:\.\w+)*)")

# Private-module imports that shouldn't appear in downstream code.
PRIVATE_IMPORT_PATHS: dict[str, str] = {
    DEPRECATED_TYPES_ROOT: (
        "deprecated path — the generated tree lives at adcp.types.domains under "
        "the same module stems; --apply renames the prefix"
    ),
}


def _split_halves(stem: str) -> tuple[str, str] | None:
    """``("adcp.types.domains.brand_discovery", "adcp.types.domains.brand")`` for a
    split domain stem, ``None`` for a stem that moved whole.

    Answered by what the installed SDK ships rather than a table here, so a
    schema added to ``scripts/generate_types.py``'s ``ROOT_DISCOVERY_SCHEMAS``
    tomorrow is resolved without touching this file. The ``_discovery`` suffix is
    the generator's own, the same one ``adcp.types._generated_poc_alias`` serves
    at runtime; ``tests/test_migrate_v3_to_v4.py`` pins the two spellings equal.
    """
    if not stem or "." in stem:
        return None
    canonical = f"{CANONICAL_TYPES_ROOT}.{stem}"
    discovery = f"{canonical}{ROOT_DISCOVERY_SUFFIX}"
    try:
        if importlib.util.find_spec(discovery) is None:
            return None
    except (ImportError, AttributeError, ValueError):
        return None
    return discovery, canonical


def _resolve_split_symbol(stem: str, symbol: str) -> str | None:
    """The half of a split stem that binds ``symbol``; discovery half first.

    Discovery first because that is the surface the pre-9.0 module had — a name
    both halves bind (``Asset``, ``Fonts``) meant the discovery schema's class
    to code written against ``generated_poc.brand``.
    """
    halves = _split_halves(stem)
    if halves is None:
        return None
    for module_name in halves:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        if hasattr(module, symbol):
            return module_name
    return None


def _split_hint(stem: str, symbols: list[str]) -> str:
    halves = _split_halves(stem) or (
        f"{CANONICAL_TYPES_ROOT}.{stem}_discovery",
        f"{CANONICAL_TYPES_ROOT}.{stem}",
    )
    names = ", ".join(symbols) if symbols else "the names used"
    return (
        f"`{stem}` split in two in 9.0: {halves[0]} holds the classes the old module "
        f"declared and {halves[1]} the ones its domain aggregates. Resolve {names} by "
        f"hand (see docs/types-9-migration.md)."
    )


def _import_symbols(symbols_text: str) -> tuple[list[str], list[str]]:
    """``("A", "B as C")`` → raw entries and the names they import."""
    raws = [raw.strip() for raw in symbols_text.split(",") if raw.strip()]
    return raws, [raw.split(" as ", 1)[0].strip() for raw in raws]


def _plan_from_import(
    line: str, match: re.Match[str]
) -> tuple[dict[str, list[str]] | None, str | None]:
    """Where a ``from adcp.types.generated_poc.<module> import ...`` line goes.

    Returns ``(targets, None)`` — new module path → raw import entries, one key
    for a whole-stem rename, two when a split stem's names resolve to different
    halves — or ``(None, hint)`` when a split stem's names cannot be resolved,
    or the line would have to be split and is not alone on its line.
    """
    module = match.group("module") or ""
    raws, symbols = _import_symbols(match.group("symbols"))
    if not module:
        # ``from adcp.types.generated_poc import brand`` names subpackages; a
        # split stem among them has no single new module.
        split = [symbol for symbol in symbols if _split_halves(symbol) is not None]
        if split:
            return None, _split_hint(split[0], [])
        return {CANONICAL_TYPES_ROOT: raws}, None
    if _split_halves(module) is None:
        return {f"{CANONICAL_TYPES_ROOT}.{module}": raws}, None
    grouped: dict[str, list[str]] = {}
    unresolved: list[str] = []
    for raw, symbol in zip(raws, symbols, strict=True):
        target = _resolve_split_symbol(module, symbol)
        if target is None:
            unresolved.append(symbol)
        else:
            grouped.setdefault(target, []).append(raw)
    if unresolved or not grouped:
        return None, _split_hint(module, unresolved)
    if len(grouped) > 1 and line[: match.start()].strip():
        # Two import statements are needed and the ``from`` shares its line
        # with other code; a textual split would corrupt it.
        return None, _split_hint(module, symbols)
    return grouped, None


def _is_split_reference(rest: str) -> bool:
    """``.brand`` (exactly the split stem) is ambiguous; ``.brand.x`` is not."""
    return rest.count(".") == 1 and _split_halves(rest[1:]) is not None


def _lift_hint(module: str, symbols: list[str]) -> str | None:
    """Name the ``adcp.types`` home of any symbol that has one, for the report."""
    lifted = [
        f"{symbol} → {replacement}"
        for symbol in symbols
        if (replacement := _generated_symbol_replacement(module, symbol)) is not None
    ]
    if not lifted:
        return None
    return "also bound on the flat surface (--auto-apply imports from there): " + ", ".join(lifted)


def _prefix_rename_findings(path: Path, lineno: int, line: str) -> list[Finding]:
    """The ``rename_import`` (or split ``flag_private``) findings for one line."""
    findings: list[Finding] = []
    column = line.index(DEPRECATED_TYPES_ROOT) + 1
    from_match = _GENERATED_POC_FROM_IMPORT.search(line)
    if from_match is not None:
        module = from_match.group("module") or ""
        _raws, symbols = _import_symbols(from_match.group("symbols"))
        targets, hint = _plan_from_import(line, from_match)
        before = f"{DEPRECATED_TYPES_ROOT}.{module}" if module else DEPRECATED_TYPES_ROOT
        if targets is None:
            findings.append(
                Finding(
                    kind="flag_private",
                    path=str(path),
                    line=lineno,
                    column=column,
                    before=before,
                    hint=hint,
                    migration_anchor="generated_poc-brand-split",
                )
            )
            return findings
        for target in targets:
            findings.append(
                Finding(
                    kind="rename_import",
                    path=str(path),
                    line=lineno,
                    column=column,
                    before=before,
                    after=target,
                    hint=_lift_hint(module, symbols),
                )
            )
        return findings
    for match in _GENERATED_POC_REF.finditer(line):
        rest = match.group("rest")
        if _is_split_reference(rest):
            findings.append(
                Finding(
                    kind="flag_private",
                    path=str(path),
                    line=lineno,
                    column=match.start() + 1,
                    before=match.group(0),
                    hint=_split_hint(rest[1:], []),
                    migration_anchor="generated_poc-brand-split",
                )
            )
        else:
            findings.append(
                Finding(
                    kind="rename_import",
                    path=str(path),
                    line=lineno,
                    column=match.start() + 1,
                    before=match.group(0),
                    after=CANONICAL_TYPES_ROOT + rest,
                )
            )
    return findings


def _prefix_rename_line(text_line: str) -> str:
    """Apply the import-path rename to one line; a flagged reference is left alone."""
    from_match = _GENERATED_POC_FROM_IMPORT.search(text_line)
    if from_match is not None:
        targets, _hint = _plan_from_import(text_line, from_match)
        if targets is None:
            return text_line
        head = text_line[: from_match.start()]
        tail = text_line[from_match.end() :]
        if len(targets) == 1:
            (target,) = targets
            symbols_text = from_match.group("symbols")
            return f"{head}from {target} import {symbols_text}{tail}"
        newline = "\r\n" if tail.endswith("\r\n") else "\n"
        statements = [
            f"{head}from {target} import {', '.join(raws)}" for target, raws in targets.items()
        ]
        return newline.join(statements) + tail
    return _GENERATED_POC_REF.sub(
        lambda match: (
            match.group(0)
            if _is_split_reference(match.group("rest"))
            else CANONICAL_TYPES_ROOT + match.group("rest")
        ),
        text_line,
    )


# Per-symbol mapping for the most common ``generated_poc`` reach-ins
# salesagent surfaced during their v3→v4 experiment (and any other
# adopter would hit). The codemod scans for ``from
# adcp.types.generated_poc.<path> import <Symbol>`` lines and emits an
# explicit "before → after" hint per symbol so adopters don't have to
# hand-grep the public-API module to find the canonical alias.
#
# Mapping shape: ``<symbol-name> → adcp.types.<symbol-name>``. Every
# symbol listed here is already exported from ``adcp.types``; the
# ``test_generated_poc_symbol_map_covers_publicly_exported_names`` test
# guards drift between this map and the SDK's public surface.
#
# Intentionally NOT in the map (yet): ``CreditLimit``, ``Setup``,
# ``GovernanceAgent``. These names appear in 8+ generated files
# (``core/account.py``, ``account/sync_accounts_response.py``,
# ``media_buy/sync_event_sources_response.py``, ``bundled/...``) — the
# codegen emits one independent class per containing schema, so a
# blanket "import from adcp.types" hint would be ambiguous about
# which variant. Adopters reaching for these get the generic
# private-module flag; landing them in the public API is a separate
# design decision (which canonical variant to expose / whether to
# expose schema-namespaced aliases like ``AccountSetup``).
GENERATED_POC_SYMBOL_MAP: dict[str, str] = {
    "AccountReference": "adcp.types.AccountReference",
    "BrandReference": "adcp.types.BrandReference",
    "ContextObject": "adcp.types.ContextObject",
    "CreativeAsset": "adcp.types.CreativeAsset",
    "Error": "adcp.types.Error",
    "MediaBuyStatus": "adcp.types.MediaBuyStatus",
    "ProductFilters": "adcp.types.ProductFilters",
    "ReportingWebhook": "adcp.types.ReportingWebhook",
}

# Collision-safe mappings whose replacement depends on the defining private
# module. Numbered codegen names cannot be mapped safely by symbol alone: the
# same bare name may describe a different schema in another generated module.
GENERATED_POC_SOURCE_SYMBOL_MAP: dict[tuple[str, str], str] = {
    ("core.account", "GovernanceAgent"): "adcp.types.CoreGovernanceAgent",
    ("core.account_ref", "AccountReference1"): "adcp.types.AccountIdReference",
    ("core.account_ref", "AccountReference2"): "adcp.types.InlineAccountReference",
    ("core.creative_asset", "CreativeAsset"): "adcp.types.LegacyCreativeAsset",
    ("core.creative_filters", "CreativeFilters"): "adcp.types.LegacyCreativeFilters",
    ("core.product_filters", "Country"): "adcp.types.ProductFilterCountry",
    ("core.product_format_declaration", "ProductFormatDeclaration"): (
        "adcp.types.LegacyProductFormatDeclaration"
    ),
    ("core.property", "Identifier"): "adcp.types.PropertyIdentifier",
    ("core.vendor_pricing_option", "VendorPricingOption"): ("adcp.types.VendorPricingOptionUnion"),
    ("core.vendor_pricing_option", "VendorPricingOption1"): ("adcp.types.CpmVendorPricingOption"),
    ("core.vendor_pricing_option", "VendorPricingOption2"): (
        "adcp.types.PercentOfMediaVendorPricingOption"
    ),
    ("core.vendor_pricing_option", "VendorPricingOption3"): (
        "adcp.types.FlatFeeVendorPricingOption"
    ),
    ("core.vendor_pricing_option", "VendorPricingOption4"): (
        "adcp.types.PerUnitVendorPricingOption"
    ),
    ("core.vendor_pricing_option", "VendorPricingOption5"): (
        "adcp.types.CustomVendorPricingOption"
    ),
    ("core.product_allocation", "ProductAllocation"): "adcp.types.ProductAllocation",
    ("core.product", "TrustedMatch"): "adcp.types.TrustedMatch",
    ("core.signal_coverage_forecast", "SignalCoverageForecast"): (
        "adcp.types.SignalCoverageForecast"
    ),
    ("signals.get_signals_response", "Range"): "adcp.types.SignalCoverageRange",
    ("core.missing_metric", "MissingMetric"): "adcp.types.MissingMetric",
    ("media_buy.update_media_buy_response", "UpdateMediaBuyResponse2"): (
        "adcp.types.LegacyUpdateMediaBuyErrorResponse"
    ),
    ("media_buy.update_media_buy_response", "UpdateMediaBuyResponse1"): (
        "adcp.types.LegacyUpdateMediaBuySuccessResponse"
    ),
    ("creative.list_creatives_request", "Sort"): "adcp.types.ListCreativesSort",
    ("signals.get_signals_response", "Signal"): "adcp.types.GetSignalsSignal",
}


# ``from adcp.types.generated_poc.<...> import <Symbol[, ...]>`` —
# captures the symbol list so we can emit per-symbol replacement hints.
# Multiline imports (parenthesized) aren't covered by this regex; they
# fall through to the generic "private module" flag, which still
# surfaces the issue and prints the migration anchor.
_GENERATED_POC_FROM_IMPORT = re.compile(
    r"from\s+adcp\.types\.generated_poc"
    r"(?:\.(?P<module>[\w.]+))?\s+import\s+(?P<symbols>[\w \t,]+)"
)


def _proposed_generated_symbol_replacement(module: str, symbol: str) -> str | None:
    return GENERATED_POC_SOURCE_SYMBOL_MAP.get((module, symbol)) or GENERATED_POC_SYMBOL_MAP.get(
        symbol
    )


def _replacement_is_identical(module: str, symbol: str, replacement: str) -> bool:
    """Verify that a private class and its proposed public target are identical.

    The spelling this codemod DETECTS is the v3 one,
    ``adcp.types.generated_poc.<module>``. The module it IMPORTS is where those
    classes live now: the generated tree moved to ``adcp.types.domains`` and
    almost every module stem came with it unchanged, so the same ``<module>``
    resolves. Importing the v3 path here would raise ``ModuleNotFoundError``,
    the except clause would swallow it, and every per-symbol rewrite would
    silently degrade to a flag.

    The one stem that split is ``brand``: ``brand.json``'s classes are at
    ``adcp.types.domains.brand_discovery`` while ``adcp.types.domains.brand``
    is the domain aggregator. Neither symbol map holds one of those classes
    today, so nothing degrades — but a symbol added from that schema would
    take the ``AttributeError`` branch and be flagged rather than rewritten,
    and the fix is to resolve it against the discovery half, not to widen the
    except clause.
    """
    if not module or not replacement.startswith("adcp.types."):
        return False
    try:
        source_module = importlib.import_module(f"adcp.types.domains.{module}")
        public_module = importlib.import_module("adcp.types")
        source = getattr(source_module, symbol)
        public = getattr(public_module, replacement.removeprefix("adcp.types."))
    except (AttributeError, ImportError):
        return False
    return source is public


def _generated_symbol_replacement(module: str, symbol: str) -> str | None:
    replacement = _proposed_generated_symbol_replacement(module, symbol)
    if replacement is None or not _replacement_is_identical(module, symbol, replacement):
        return None
    return replacement


def _unsafe_replacement_hint(module: str, symbol: str, replacement: str) -> str:
    source = f"adcp.types.generated_poc.{module}.{symbol}"
    return f"SKIP: source {source} is not identical to {replacement} — manual rewrite required"


# Regex for numbered Assets direct imports (``Assets5``, ``Assets14``, etc).
# Bare ``Assets`` (no digits) is a legitimate base class alias; the
# regex requires at least one digit to avoid false positives.
NUMBERED_ASSETS_PATTERN = re.compile(r"\bAssets\d+\b")


# Numbered-Assets → public semantic alias mapping.  Derived from the
# ``Assets<N>`` → ``<Type>FormatAsset`` assignments in
# ``adcp.types.aliases``.  Entries here are auto-applicable under
# ``--auto-apply``; anything not listed stays ``flag_numbered`` and
# requires human review.
#
# Stability contract: ``tests/test_asset_aliases_stable.py`` guards that
# each alias resolves to the correct ``asset_type`` literal.  Generator
# renumbering is caught there, not in downstream code.
NUMBERED_ASSETS_RENAMES: dict[str, str] = {
    "Assets81": "VideoFormatAsset",
    "Assets82": "AudioFormatAsset",
    "Assets83": "TextFormatAsset",
    "Assets84": "MarkdownFormatAsset",
    "Assets85": "HtmlFormatAsset",
    "Assets86": "CssFormatAsset",
    "Assets87": "JavascriptFormatAsset",
    "Assets88": "VastFormatAsset",
    "Assets89": "DaastFormatAsset",
    "Assets90": "UrlFormatAsset",
    "Assets91": "WebhookFormatAsset",
    "Assets92": "BriefFormatAsset",
    "Assets93": "CatalogFormatAsset",
    "Assets94": "RepeatableAssetGroup",
    "Assets95": "ImageFormatGroupAsset",
    "Assets96": "VideoFormatGroupAsset",
    "Assets97": "AudioFormatGroupAsset",
    "Assets98": "TextFormatGroupAsset",
    "Assets99": "MarkdownFormatGroupAsset",
    "Assets100": "HtmlFormatGroupAsset",
    "Assets101": "CssFormatGroupAsset",
    "Assets102": "JavascriptFormatGroupAsset",
    "Assets103": "VastFormatGroupAsset",
    "Assets104": "DaastFormatGroupAsset",
    "Assets105": "UrlFormatGroupAsset",
    "Assets106": "WebhookFormatGroupAsset",
}


@dataclass
class Finding:
    """One migration finding — either an applied rename or a manual TODO."""

    # Valid kind values: "rename" | "rename_import" | "auto_applied" |
    #   "flag_removed" | "flag_private" | "flag_numbered" |
    #   "flag_attribute" | "flag_enum_value"
    kind: str
    path: str
    line: int
    column: int
    before: str
    after: str | None = None  # None for flag-only items
    hint: str | None = None
    migration_anchor: str | None = None


@dataclass
class Report:
    """Structured migration report."""

    applied: list[Finding] = field(default_factory=list)
    auto_applied: list[Finding] = field(default_factory=list)
    flagged: list[Finding] = field(default_factory=list)
    scanned_files: int = 0
    rewritten_files: int = 0

    def add(self, finding: Finding) -> None:
        if finding.kind in ("rename", "rename_import"):
            self.applied.append(finding)
        elif finding.kind == "auto_applied":
            self.auto_applied.append(finding)
        else:
            self.flagged.append(finding)


_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        ".eggs",
    }
)


def _iter_python_files(root: Path) -> list[Path]:
    """Walk ``root`` for ``*.py`` files, skipping common build/dep dirs.

    Skip-dir matching is applied to path components *relative to
    ``root``*, not absolute parts. A seller's repo checked out at
    ``/home/ci/build/myrepo/src`` (where ``build`` is a CI-scratch
    ancestor directory) previously had every file silently skipped —
    the absolute-path check hit ``build`` and dropped the whole tree.
    Relative matching makes the skip honour user intent: skip
    ``myrepo/src/build/output.py`` while still scanning
    ``/home/ci/build/myrepo/src/app.py``.
    """
    if root.is_file():
        return [root] if root.suffix == ".py" else []
    resolved_root = root.resolve()
    files: list[Path] = []
    for p in root.rglob("*.py"):
        try:
            rel_parts = p.resolve().relative_to(resolved_root).parts
        except ValueError:
            # rglob can return paths outside root when root contains a
            # symlink; fall back to the raw parts for those.
            rel_parts = p.parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        files.append(p)
    return sorted(files)


# Compile rename regexes once at module import. Word boundaries prevent
# partial matches (``MyAudioAsset`` stays untouched).
_RENAME_PATTERNS = {name: re.compile(rf"\b{re.escape(name)}\b") for name in ASSET_CONTENT_RENAMES}
_REMOVED_PATTERNS = {name: re.compile(rf"\b{re.escape(name)}\b") for name in REMOVED_TYPES}

# Attribute access patterns — word-boundary regex prevents
# ``my.brand_manifest_v2`` / ``brand_manifest_foo`` false positives
# that a plain ``in`` substring check would fire on.
_REMOVED_ATTRIBUTE_PATTERNS = {
    attr: re.compile(rf"{re.escape(attr)}\b") for attr in REMOVED_ATTRIBUTE_ACCESSES
}

# Enum value patterns — re.escape handles the dot so the pattern matches
# the literal ``MediaBuyStatus.pending_activation``, not a regex wildcard.
_REMOVED_ENUM_VALUE_PATTERNS = {
    val: re.compile(rf"{re.escape(val)}\b") for val in REMOVED_ENUM_VALUES
}

# Compiled patterns for the numbered-assets rename table.
_NUMBERED_RENAME_PATTERNS = {
    name: re.compile(rf"\b{re.escape(name)}\b") for name in NUMBERED_ASSETS_RENAMES
}


def scan_file(
    path: Path, *, apply_changes: bool, auto_apply: bool = False
) -> tuple[list[Finding], str | None]:
    """Scan one file. Returns (findings, new_contents_or_None).

    new_contents_or_None is None when apply_changes=False or when no
    renames fired; the caller uses it as the signal to rewrite.

    ``auto_apply=True`` promotes safe findings to ``kind="auto_applied"``
    in the returned list, but file rewrites only happen when
    ``apply_changes=True`` as well — ``auto_apply`` alone never writes.

    Reads with ``utf-8-sig`` so UTF-8-BOM-prefixed source files (legal
    Python, common on Windows) migrate correctly. Uses ``newline=""``
    on read and write so CRLF line endings are preserved verbatim —
    Windows sellers otherwise get a giant noise diff where every line
    flips to LF.
    """
    findings: list[Finding] = []
    try:
        # Use ``open(..., newline="")`` over ``Path.read_text(newline=)``
        # — the latter was added in 3.13 but the SDK supports 3.10+.
        with open(path, encoding="utf-8-sig", newline="") as fh:
            original = fh.read()
    except (UnicodeDecodeError, OSError):
        # Skip unreadable or non-UTF8 files; migration targets Python source.
        return findings, None

    # Detect renames per-line so the report carries column info and the
    # same pattern that matched detection also drives the rewrite.
    updated = original
    rename_hits = False
    auto_apply_hits = False  # any numbered or private-import rewrites queued
    rename_import_hits = False  # any generated_poc → domains path renames queued

    for lineno, line in enumerate(original.splitlines(), start=1):
        # Pre-pass: when this line is a single-line ``generated_poc``
        # import, decide whether the line as a whole is auto-apply-safe.
        # An import is *unsafe* when at least one of its symbols (after
        # the hypothetical numbered substitution) isn't in
        # ``_AUTO_APPLY_PUBLIC_SYMBOLS``; rewriting one symbol while
        # leaving another behind would leave the line importing a
        # public name from a private module — guaranteed ImportError.
        # The rewrite block (`updated.splitlines()` later) skips
        # unsafe-mixed lines; the per-symbol Finding emission below
        # also treats numbered references on those lines as
        # ``flag_numbered`` rather than ``auto_applied`` so the report
        # matches the file content.
        line_is_mixed_unsafe_import = False
        if "adcp.types.generated_poc" in line:
            from_match = _GENERATED_POC_FROM_IMPORT.search(line)
            if from_match:
                module = from_match.group("module") or ""
                raw_syms = [s.strip() for s in from_match.group("symbols").split(",")]
                pre_syms = [r.split(" as ")[0].strip() for r in raw_syms if r.strip()]
                if pre_syms and not all(
                    _generated_symbol_replacement(module, symbol) is not None
                    or symbol in NUMBERED_ASSETS_RENAMES
                    for symbol in pre_syms
                ):
                    line_is_mixed_unsafe_import = True

        for old, new in ASSET_CONTENT_RENAMES.items():
            for match in _RENAME_PATTERNS[old].finditer(line):
                findings.append(
                    Finding(
                        kind="rename",
                        path=str(path),
                        line=lineno,
                        column=match.start() + 1,
                        before=old,
                        after=new,
                    )
                )
                rename_hits = True

        # Removed types — flagged, not rewritten.
        for name, (hint, anchor) in REMOVED_TYPES.items():
            for match in _REMOVED_PATTERNS[name].finditer(line):
                findings.append(
                    Finding(
                        kind="flag_removed",
                        path=str(path),
                        line=lineno,
                        column=match.start() + 1,
                        before=name,
                        hint=hint,
                        migration_anchor=anchor,
                    )
                )

        # Numbered Assets imports / references.
        for match in NUMBERED_ASSETS_PATTERN.finditer(line):
            symbol = match.group(0)
            alias = NUMBERED_ASSETS_RENAMES.get(symbol)
            if auto_apply and alias is not None and not line_is_mixed_unsafe_import:
                findings.append(
                    Finding(
                        kind="auto_applied",
                        path=str(path),
                        line=lineno,
                        column=match.start() + 1,
                        before=symbol,
                        after=alias,
                    )
                )
                auto_apply_hits = True
            else:
                findings.append(
                    Finding(
                        kind="flag_numbered",
                        path=str(path),
                        line=lineno,
                        column=match.start() + 1,
                        before=symbol,
                        after=alias,  # hint toward the public alias even in flag mode
                        hint=(
                            "numbered Assets classes are unstable across spec revisions; "
                            "import the semantic alias from adcp.types instead"
                        ),
                        migration_anchor="numbered-discriminated-union-classes-shifted",
                    )
                )

        # adcp.types.generated_poc references (#1417).
        #
        # Under ``--auto-apply``, a single-line
        #   ``from adcp.types.generated_poc.<path> import <symbols>``
        # whose every symbol is a known public alias (or a mapped numbered
        # asset) is lifted to ``adcp.types`` — one ``auto_applied`` Finding
        # per symbol. Every other reference is a prefix rename to
        # ``adcp.types.domains`` (``rename_import``, applied by ``--apply``),
        # except a bare split stem (``generated_poc.brand``) whose names
        # cannot be placed, which is flagged with the two halves named.
        #
        # Numbered-Assets imports appear on these lines too; the numbered
        # pass above owns their Findings.
        if DEPRECATED_TYPES_ROOT in line:
            from_match = _GENERATED_POC_FROM_IMPORT.search(line)
            lifted = False
            if from_match is not None and auto_apply and not line_is_mixed_unsafe_import:
                module = from_match.group("module") or ""
                _raws, symbols = _import_symbols(from_match.group("symbols"))
                lifted = bool(symbols)
                for symbol in symbols:
                    replacement = _generated_symbol_replacement(module, symbol)
                    if replacement is None:
                        continue  # a mapped numbered asset; the numbered pass reported it
                    sym_col = line.find(symbol, from_match.start(1)) + 1
                    findings.append(
                        Finding(
                            kind="auto_applied",
                            path=str(path),
                            line=lineno,
                            column=(
                                sym_col if sym_col > 0 else line.index(DEPRECATED_TYPES_ROOT) + 1
                            ),
                            before=symbol,
                            after=replacement,
                            hint=(
                                "deprecated path — import "
                                f"{replacement.rsplit('.', 1)[-1]} from "
                                "adcp.types (stable public API) instead"
                            ),
                        )
                    )
                    auto_apply_hits = True
            if not lifted:
                for finding in _prefix_rename_findings(path, lineno, line):
                    findings.append(finding)
                    if finding.kind == "rename_import":
                        rename_import_hits = True

        # Removed attribute accesses (.brand_manifest etc.). Regex with
        # trailing word boundary prevents false-positives on
        # ``.brand_manifest_v2``, ``.brand_manifest_override``, etc.
        for attr, hint in REMOVED_ATTRIBUTE_ACCESSES.items():
            for match in _REMOVED_ATTRIBUTE_PATTERNS[attr].finditer(line):
                findings.append(
                    Finding(
                        kind="flag_attribute",
                        path=str(path),
                        line=lineno,
                        column=match.start() + 1,
                        before=attr,
                        hint=hint,
                    )
                )

        # Removed enum values (e.g. MediaBuyStatus.pending_activation). The
        # class-qualified form is anchored tightly enough that false positives
        # are unlikely; trailing word boundary prevents suffix matches like
        # ``MediaBuyStatus.pending_activation_v2``.
        for enum_val, (enum_hint, enum_anchor) in REMOVED_ENUM_VALUES.items():
            for match in _REMOVED_ENUM_VALUE_PATTERNS[enum_val].finditer(line):
                findings.append(
                    Finding(
                        kind="flag_enum_value",
                        path=str(path),
                        line=lineno,
                        column=match.start() + 1,
                        before=enum_val,
                        hint=enum_hint,
                        migration_anchor=enum_anchor,
                    )
                )

    needs_write = False

    if apply_changes and rename_hits:
        for old, new in ASSET_CONTENT_RENAMES.items():
            updated = _RENAME_PATTERNS[old].sub(new, updated)
        needs_write = True

    if apply_changes and auto_apply and auto_apply_hits:
        # Process the file line-by-line so generated_poc imports get a
        # safety check against the post-numbered-substitution symbol set
        # before any rewrite happens. The earlier "Step 1: substitute
        # Assets<N> file-wide; Step 2: fix import paths only when safe"
        # ordering corrupted mixed lines like
        # ``from generated_poc.core.format import Assets81, Assets149``
        # — Assets81 became VideoFormatAsset while Assets149 stayed,
        # leaving VideoFormatAsset imported from a private module.
        new_lines: list[str] = []
        for text_line in updated.splitlines(keepends=True):
            is_generated_poc_import = (
                "adcp.types.generated_poc" in text_line
                and _GENERATED_POC_FROM_IMPORT.search(text_line) is not None
            )
            if is_generated_poc_import:
                m = _GENERATED_POC_FROM_IMPORT.search(text_line)
                assert m is not None  # narrowed above
                module = m.group("module") or ""
                raw_syms = [s.strip() for s in m.group("symbols").split(",")]
                pre_syms = [r.split(" as ")[0].strip() for r in raw_syms if r.strip()]
                replacements = [
                    _generated_symbol_replacement(module, symbol)
                    or (
                        f"adcp.types.{NUMBERED_ASSETS_RENAMES[symbol]}"
                        if symbol in NUMBERED_ASSETS_RENAMES
                        else None
                    )
                    for symbol in pre_syms
                ]
                if replacements and all(replacement is not None for replacement in replacements):
                    public_imports: list[str] = []
                    for raw, symbol, replacement in zip(
                        raw_syms, pre_syms, replacements, strict=True
                    ):
                        assert replacement is not None
                        public_name = replacement.rsplit(".", 1)[-1]
                        local_name = raw.split(" as ", 1)[1].strip() if " as " in raw else None
                        if local_name is not None:
                            public_imports.append(f"{public_name} as {local_name}")
                        elif symbol in NUMBERED_ASSETS_RENAMES:
                            public_imports.append(public_name)
                        elif public_name != symbol:
                            # Preserve existing use sites while moving the import
                            # to the semantic public name.
                            public_imports.append(f"{public_name} as {symbol}")
                        else:
                            public_imports.append(public_name)
                    replacement_import = "from adcp.types import " + ", ".join(public_imports)
                    text_line = text_line[: m.start()] + replacement_import + text_line[m.end() :]
                # Mixed line — leave it alone. The findings list still
                # carries the per-symbol flag_private and flag_numbered
                # entries so the adopter sees the work to do.
                new_lines.append(text_line)
                continue
            # Non-import lines: substitute numbered names freely (the
            # semantic alias is already importable via adcp.types and
            # any local reference the line carries is a usage site).
            for old, new in NUMBERED_ASSETS_RENAMES.items():
                text_line = _NUMBERED_RENAME_PATTERNS[old].sub(new, text_line)
            new_lines.append(text_line)
        updated = "".join(new_lines)
        needs_write = True

    if apply_changes and rename_import_hits and DEPRECATED_TYPES_ROOT in updated:
        # After any lift to ``adcp.types`` above, every remaining
        # ``generated_poc`` reference is a prefix rename (or a flagged split
        # stem, which ``_prefix_rename_line`` leaves alone).
        updated = "".join(
            _prefix_rename_line(text_line) if DEPRECATED_TYPES_ROOT in text_line else text_line
            for text_line in updated.splitlines(keepends=True)
        )
        needs_write = True

    if needs_write:
        return findings, updated
    return findings, None


def run(root: Path, *, apply_changes: bool = False, auto_apply: bool = False) -> Report:
    """Execute the migration across ``root``. Returns a :class:`Report`."""
    report = Report()
    for path in _iter_python_files(root):
        report.scanned_files += 1
        findings, new_contents = scan_file(path, apply_changes=apply_changes, auto_apply=auto_apply)
        for f in findings:
            report.add(f)
        if new_contents is not None:
            # newline="" preserves whatever line endings were read
            # (including mixed — unusual but possible). Pair with the
            # ``open(..., newline="")`` read in ``scan_file``.
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(new_contents)
            report.rewritten_files += 1
    return report


def _format_text_report(report: Report, *, apply_changes: bool, auto_apply: bool = False) -> str:
    """Human-readable migration report for the default CLI output."""
    lines: list[str] = []
    mode = "applied" if apply_changes else "would apply"

    lines.append(f"adcp migrate v3-to-v4 — scanned {report.scanned_files} files")
    lines.append("")

    asset_renames = [f for f in report.applied if f.kind == "rename"]
    import_renames = [f for f in report.applied if f.kind == "rename_import"]
    if asset_renames:
        lines.append(f"Asset renames {mode}: {len(asset_renames)}")
        # Group by (before, after) for a compact summary.
        by_rename: dict[str, dict[str, list[Finding]]] = {}
        for f in asset_renames:
            by_rename.setdefault(f.before, {}).setdefault(f.after or "?", []).append(f)
        for before, after_map in sorted(by_rename.items()):
            for after, hits in sorted(after_map.items()):
                lines.append(
                    f"  {before} → {after}  ({len(hits)} hit{'s' if len(hits) != 1 else ''})"
                )
                for f in hits[:5]:
                    lines.append(f"    {f.path}:{f.line}:{f.column}")
                if len(hits) > 5:
                    lines.append(f"    … and {len(hits) - 5} more")
    else:
        lines.append("No asset renames needed.")

    if import_renames:
        lines.append("")
        lines.append(f"Import path renames {mode}: {len(import_renames)}")
        by_path: dict[str, dict[str, list[Finding]]] = {}
        for f in import_renames:
            by_path.setdefault(f.before, {}).setdefault(f.after or "?", []).append(f)
        for before, after_map in sorted(by_path.items()):
            for after, hits in sorted(after_map.items()):
                lines.append(
                    f"  {before} → {after}  ({len(hits)} hit{'s' if len(hits) != 1 else ''})"
                )
                hint = hits[0].hint
                if hint:
                    lines.append(f"    → {hint}")
                for f in hits[:5]:
                    lines.append(f"    {f.path}:{f.line}:{f.column}")
                if len(hits) > 5:
                    lines.append(f"    … and {len(hits) - 5} more")

    if report.auto_applied:
        lines.append("")
        lines.append(f"Safe rewrites {mode}: {len(report.auto_applied)}")
        by_name: dict[str, dict[str, list[Finding]]] = {}
        for f in report.auto_applied:
            by_name.setdefault(f.before, {}).setdefault(f.after or "?", []).append(f)
        for before, after_map in sorted(by_name.items()):
            for after, hits in sorted(after_map.items()):
                lines.append(
                    f"  {before} → {after}  ({len(hits)} hit{'s' if len(hits) != 1 else ''})"
                )
                for f in hits[:5]:
                    lines.append(f"    {f.path}:{f.line}:{f.column}")
                if len(hits) > 5:
                    lines.append(f"    … and {len(hits) - 5} more")

    if report.flagged:
        lines.append("")
        lines.append(f"Manual review required: {len(report.flagged)} findings")
        by_flagged: dict[str, list[Finding]] = {}
        for f in report.flagged:
            by_flagged.setdefault(f.before, []).append(f)
        for name, hits in sorted(by_flagged.items()):
            # Per-symbol mapping ("ContextObject → adcp.types.ContextObject")
            # — print the explicit replacement on the header line so
            # adopters fix without leaving the report. Falls back to
            # bare name when no replacement is mapped.
            replacement = hits[0].after
            header = (
                f"  {name} → {replacement}  ({len(hits)} hit{'s' if len(hits) != 1 else ''})"
                if replacement
                else f"  {name}  ({len(hits)} hit{'s' if len(hits) != 1 else ''})"
            )
            lines.append(header)
            hint = hits[0].hint
            if hint:
                lines.append(f"    → {hint}")
            anchor = hits[0].migration_anchor
            if anchor:
                lines.append(f"    MIGRATION_v3_to_v4.md#{anchor}")
            for f in hits[:5]:
                lines.append(f"    {f.path}:{f.line}:{f.column}")
            if len(hits) > 5:
                lines.append(f"    … and {len(hits) - 5} more")
    else:
        lines.append("")
        lines.append("No manual-review findings.")

    if (apply_changes or auto_apply) and report.rewritten_files:
        lines.append("")
        lines.append(f"Rewrote {report.rewritten_files} files in place.")
        lines.append("Review with `git diff` before committing.")

    if not auto_apply and any(f.kind == "flag_numbered" for f in report.flagged):
        lines.append("")
        lines.append(
            "Tip: rerun with --auto-apply to mechanically fix the flag_numbered "
            "findings above and import from adcp.types where a name is bound there."
        )

    return "\n".join(lines)


REPORT_SCHEMA_VERSION = 1
"""Version of the JSON report shape. CI scripts / editors parsing the
migrate output key on this so a future shape change (adding a summary
block, renaming fields) doesn't silently break them.

Bump the minor SDK version AND this constant when changing the JSON
shape in a non-additive way. Additive changes (new optional keys)
stay at the same version.

**v1 shape:**

.. code-block:: json

    {
      "schema_version": 1,
      "scanned_files": int,
      "rewritten_files": int,
      "applied": [
        {"kind": "rename" | "rename_import", "path": str, "line": int,
         "column": int, "before": str, "after": str, "hint": str | null,
         "migration_anchor": null}
      ],
      "auto_applied": [
        {"kind": "auto_applied", "path": str, "line": int, "column": int,
         "before": str, "after": str, "hint": str | null, "migration_anchor": null}
      ],
      "flagged": [
        {"kind": "flag_removed" | "flag_numbered" | "flag_private"
                 | "flag_attribute" | "flag_enum_value",
         "path": str, "line": int, "column": int, "before": str,
         "after": str | null, "hint": str | null, "migration_anchor": str | null}
      ]
    }

``auto_applied`` is an additive field (v1, no version bump needed), and so
is the ``rename_import`` kind in ``applied`` (#1417): the
``adcp.types.generated_poc`` → ``adcp.types.domains`` import-path rename,
reported with the old module path in ``before`` and the new one in ``after``.
Parsers that don't know about it receive an empty array in non-``--auto-apply``
runs and can safely ignore it.  Entries in ``flagged`` always require
human attention regardless of what ``auto_applied`` contains.
"""


def _format_json_report(report: Report) -> str:
    """JSON report for programmatic consumption (CI, editors).

    Versioned via :data:`REPORT_SCHEMA_VERSION` — parsers should check
    the top-level ``schema_version`` key before reading the rest.
    """
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "scanned_files": report.scanned_files,
        "rewritten_files": report.rewritten_files,
        "applied": [asdict(f) for f in report.applied],
        "auto_applied": [asdict(f) for f in report.auto_applied],
        "flagged": [asdict(f) for f in report.flagged],
    }
    return json.dumps(payload, indent=2)


def _is_dirty_tree(path: Path) -> bool:
    """True when ``path`` is inside a git repo with uncommitted changes.

    Uses ``git status --porcelain`` for speed and stability. Returns
    ``False`` when git isn't installed, the path isn't in a repo, or
    the repo is clean — any non-clean state returns ``True`` so the
    ``--apply`` guard fails safe.

    The check is best-effort: absence of git isn't a reason to block
    the rewrite (sellers may run in sandboxed or read-only environments
    where git isn't available). A ``True`` result means we saw
    definite uncommitted state.
    """
    import shutil
    import subprocess

    if shutil.which("git") is None:
        return False

    target = path.resolve()
    cwd = target if target.is_dir() else target.parent
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # Exit 128 = not a git repo; anything non-zero → treat as clean
    # (not blocking — we don't want `--apply` in a sandboxed env to
    # break because git can't run).
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``python -m adcp.migrate v3-to-v4``."""
    parser = argparse.ArgumentParser(
        prog="adcp.migrate v3-to-v4",
        description=(
            "Rewrite adcp 3.x → 4.0 ``<Type>Asset`` → ``<Type>Content`` renames, "
            "rename adcp.types.generated_poc imports to adcp.types.domains, "
            "and flag usages of removed types. "
            "Exits 0 when all findings are mechanical (or none); "
            "exits 1 when flag_removed findings remain for human review; "
            "exits 2 on usage errors."
        ),
    )
    parser.add_argument(
        "path",
        type=Path,
        help="File or directory to scan (source tree root in typical use).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Rewrite files in place: the <Type>Asset renames and the "
            "adcp.types.generated_poc → adcp.types.domains import paths. "
            "Default is dry-run (report only). Commit your tree first so "
            "`git diff` is your review view. See also --auto-apply."
        ),
    )
    parser.add_argument(
        "--auto-apply",
        action="store_true",
        dest="auto_apply",
        help=(
            "Rewrite files in place (implies --apply) and additionally "
            "auto-apply safe import rewrites: a generated_poc import "
            "whose every symbol is bound on adcp.types is imported from "
            "there instead of the domain path, and flag_numbered "
            "findings with a documented semantic alias (Assets81 → "
            "VideoFormatAsset, etc.). flag_removed findings always "
            "require human review and remain flagged; exit code 1 when "
            "any remain."
        ),
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "Allow --apply / --auto-apply even when the git working tree "
            "has uncommitted changes. Default is to refuse so `git diff` "
            "after the migration shows only the codemod's rewrites, "
            "not a mix of the seller's in-progress work and the "
            "codemod. Pass --allow-dirty when you know what you're "
            "doing (e.g. applying to a staged change deliberately)."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON report instead of the human-readable text.",
    )
    args = parser.parse_args(argv)

    # --auto-apply implies --apply; treat them uniformly downstream.
    if args.auto_apply:
        args.apply = True

    if not args.path.exists():
        print(f"error: path does not exist: {args.path}", file=sys.stderr)
        return 2

    if args.apply and not args.allow_dirty and _is_dirty_tree(args.path):
        flag_used = "--auto-apply" if args.auto_apply else "--apply"
        print(
            f"error: {flag_used} refused on a dirty git working tree.\n"
            "       Commit your changes first so `git diff` after the\n"
            "       migration shows only the codemod's rewrites. Pass\n"
            "       --allow-dirty to override (e.g. you're deliberately\n"
            "       applying on top of staged changes).",
            file=sys.stderr,
        )
        return 2

    report = run(args.path, apply_changes=args.apply, auto_apply=args.auto_apply)

    if args.json:
        print(_format_json_report(report))
    else:
        print(_format_text_report(report, apply_changes=args.apply, auto_apply=args.auto_apply))

    # Return non-zero when there are manual-review findings so CI can
    # gate on a clean report. Applied/auto-applied rewrites alone don't
    # trip the gate — they're mechanical and apply cleanly.
    return 1 if report.flagged else 0


if __name__ == "__main__":
    sys.exit(main())

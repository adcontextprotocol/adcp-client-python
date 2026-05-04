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
  regex is safe; sellers should still review the diff.
* **Flagged**: removed types, numbered ``Assets<N>`` imports,
  ``adcp.types.generated_poc`` imports. These don't rewrite — the
  seller has to choose the replacement (e.g. ``BrandManifest`` →
  ``BrandReference(domain=...)`` depends on call-site context).

Invocation::

    python -m adcp.migrate v3-to-v4 ./src               # dry run, report only
    python -m adcp.migrate v3-to-v4 ./src --apply       # rewrite files in place
    python -m adcp.migrate v3-to-v4 ./src --auto-apply  # also rewrite safe imports
    python -m adcp.migrate v3-to-v4 ./src --json        # structured report

The dry run is the default — you always see what would change before
anything moves. ``--apply`` rewrites the 9 ``<Type>Asset`` renames in
place.  ``--auto-apply`` implies ``--apply`` and additionally rewrites
``flag_private`` findings whose target symbol is a known public alias in
``adcp.types``, and ``flag_numbered`` findings with a documented semantic
alias (``Assets81`` → ``VideoFormatAsset``, etc.).  ``flag_removed``
findings always require human review and remain flagged even with
``--auto-apply``.  Commit your tree before running either write mode so
``git diff`` is your review view.

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
        "use BrandReference(domain=...) on requests; " "read ResolvedBrand.brand from the registry",
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
        "use the discriminated *PricingOption classes " "(e.g. CpmFixedRatePricingOption)",
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


# Private-module imports that shouldn't appear in downstream code.
PRIVATE_IMPORT_PATHS: dict[str, str] = {
    "adcp.types.generated_poc": (
        "private module — import from adcp.types (stable public API) instead"
    ),
}


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


# ``from adcp.types.generated_poc.<...> import <Symbol[, ...]>`` —
# captures the symbol list so we can emit per-symbol replacement hints.
# Multiline imports (parenthesized) aren't covered by this regex; they
# fall through to the generic "private module" flag, which still
# surfaces the issue and prints the migration anchor.
_GENERATED_POC_FROM_IMPORT = re.compile(
    r"from\s+adcp\.types\.generated_poc(?:\.[\w.]+)?\s+import\s+([\w\s,]+)"
)


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

    # Valid kind values: "rename" | "auto_applied" | "flag_removed" |
    #   "flag_private" | "flag_numbered" | "flag_attribute" |
    #   "flag_enum_value"
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
        if finding.kind == "rename":
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

# Regex used in the ``--auto-apply`` import-path fix pass: matches the
# ``from adcp.types.generated_poc...`` prefix so it can be replaced with
# ``from adcp.types``.
_GENERATED_POC_MODULE_RE = re.compile(
    r"from\s+adcp\.types\.generated_poc(?:\.[\w.]+)?\s+import"
)

# Union of symbol names that ``--auto-apply`` can safely reroute to
# ``adcp.types``: the explicit flag_private symbol map plus every public
# alias produced by a numbered-assets rename.
_AUTO_APPLY_PUBLIC_SYMBOLS: frozenset[str] = frozenset(
    set(GENERATED_POC_SYMBOL_MAP.keys()) | set(NUMBERED_ASSETS_RENAMES.values())
)


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

    for lineno, line in enumerate(original.splitlines(), start=1):
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
            if auto_apply and alias is not None:
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

        # adcp.types.generated_poc imports.
        #
        # When the line is a single-line
        #   ``from adcp.types.generated_poc.<path> import <symbols>``
        # emit one per-symbol Finding.  For symbols in
        # GENERATED_POC_SYMBOL_MAP the Finding carries the public alias;
        # unknown symbols get the generic "private module" flag so they
        # still surface (fixing the prior silent-drop on mixed lines).
        #
        # Under ``--auto-apply`` the per-symbol findings are promoted to
        # ``auto_applied`` only when ALL symbols on the line are in the
        # map — a mixed line cannot be safely rewritten without splitting
        # the import statement, so it remains flagged.
        #
        # Numbered-Assets imports (e.g. ``import Assets81``) appear here
        # too.  Those are handled by the numbered pass above and will be
        # fixed by the post-scan import-path rewrite; suppress the extra
        # generic flag so the report doesn't double-count them.
        for private_path, hint in PRIVATE_IMPORT_PATHS.items():
            if private_path not in line:
                continue
            col = line.index(private_path) + 1
            from_match = _GENERATED_POC_FROM_IMPORT.search(line)
            if from_match:
                raw_symbols = [s.strip() for s in from_match.group(1).split(",")]
                parsed: list[tuple[str, str | None]] = []
                for raw in raw_symbols:
                    raw = raw.strip()
                    if not raw:
                        continue
                    symbol = raw.split(" as ")[0].strip()
                    if not symbol:
                        continue
                    parsed.append((symbol, GENERATED_POC_SYMBOL_MAP.get(symbol)))

                if not parsed:
                    findings.append(
                        Finding(
                            kind="flag_private",
                            path=str(path),
                            line=lineno,
                            column=col,
                            before=private_path,
                            hint=hint,
                        )
                    )
                    continue

                all_known = all(
                    repl is not None
                    or (auto_apply and symbol in NUMBERED_ASSETS_RENAMES)
                    for symbol, repl in parsed
                )

                for symbol, replacement in parsed:
                    sym_col = line.find(symbol, from_match.start(1)) + 1
                    if sym_col <= 0:
                        sym_col = col
                    if replacement is not None:
                        kind = "auto_applied" if (auto_apply and all_known) else "flag_private"
                        if kind == "auto_applied":
                            auto_apply_hits = True
                        findings.append(
                            Finding(
                                kind=kind,
                                path=str(path),
                                line=lineno,
                                column=sym_col,
                                before=symbol,
                                after=replacement,
                                hint=(
                                    f"private module — import {symbol} from "
                                    "adcp.types (stable public API) instead"
                                ),
                            )
                        )
                    else:
                        # Unknown symbol.  Suppress the generic flag when
                        # auto_apply is active and the symbol is a numbered
                        # asset that will be renamed by the other pass —
                        # the import-path fix covers it.
                        if auto_apply and symbol in NUMBERED_ASSETS_RENAMES:
                            continue
                        findings.append(
                            Finding(
                                kind="flag_private",
                                path=str(path),
                                line=lineno,
                                column=sym_col,
                                before=private_path,
                                hint=hint,
                            )
                        )
            else:
                # Multiline import, star import, or regex mismatch.
                findings.append(
                    Finding(
                        kind="flag_private",
                        path=str(path),
                        line=lineno,
                        column=col,
                        before=private_path,
                        hint=hint,
                    )
                )

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
        # Step 1: substitute Assets<N> → SemanticAlias everywhere
        # (handles both usage sites and import symbols).
        for old, new in NUMBERED_ASSETS_RENAMES.items():
            updated = _NUMBERED_RENAME_PATTERNS[old].sub(new, updated)

        # Step 2: fix any generated_poc import whose symbols are now all
        # resolvable to adcp.types.  This covers two cases:
        #
        #   a. ``from generated_poc.core.format import Assets81`` became
        #      ``from generated_poc.core.format import VideoFormatAsset``
        #      after step 1 — rewrite the module path.
        #
        #   b. ``from generated_poc.core.x import ContextObject`` whose
        #      all-known flag_private findings were promoted to auto_applied
        #      — rewrite the module path here too.
        #
        # The check uses ``_AUTO_APPLY_PUBLIC_SYMBOLS`` (a frozen set of all
        # known-safe names) so we never fix an import that still references
        # an unknown symbol.
        new_lines: list[str] = []
        for text_line in updated.splitlines(keepends=True):
            if "adcp.types.generated_poc" not in text_line:
                new_lines.append(text_line)
                continue
            m = _GENERATED_POC_FROM_IMPORT.search(text_line)
            if m:
                raw_syms = [s.strip() for s in m.group(1).split(",")]
                syms = [r.split(" as ")[0].strip() for r in raw_syms if r.strip()]
                if syms and all(sym in _AUTO_APPLY_PUBLIC_SYMBOLS for sym in syms):
                    text_line = _GENERATED_POC_MODULE_RE.sub(
                        "from adcp.types import", text_line
                    )
            new_lines.append(text_line)
        updated = "".join(new_lines)
        needs_write = True

    if needs_write:
        return findings, updated
    return findings, None


def run(root: Path, *, apply_changes: bool = False, auto_apply: bool = False) -> Report:
    """Execute the migration across ``root``. Returns a :class:`Report`."""
    report = Report()
    for path in _iter_python_files(root):
        report.scanned_files += 1
        findings, new_contents = scan_file(
            path, apply_changes=apply_changes, auto_apply=auto_apply
        )
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

    if report.applied:
        lines.append(f"Asset renames {mode}: {len(report.applied)}")
        # Group by (before, after) for a compact summary.
        by_rename: dict[str, dict[str, list[Finding]]] = {}
        for f in report.applied:
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

    if not auto_apply and any(
        f.kind in ("flag_private", "flag_numbered") for f in report.flagged
    ):
        lines.append("")
        lines.append(
            "Tip: rerun with --auto-apply to mechanically fix the "
            "flag_private and flag_numbered findings above."
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
        {"kind": "rename", "path": str, "line": int, "column": int,
         "before": str, "after": str, "hint": null, "migration_anchor": null}
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

``auto_applied`` is an additive field (v1, no version bump needed).
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
            "Rewrite adcp 3.x → 4.0 ``<Type>Asset`` → ``<Type>Content`` renames "
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
            "Rewrite files in place. Default is dry-run (report only). "
            "Commit your tree first so `git diff` is your review view. "
            "See also --auto-apply to also mechanically fix flag_private "
            "and flag_numbered findings."
        ),
    )
    parser.add_argument(
        "--auto-apply",
        action="store_true",
        dest="auto_apply",
        help=(
            "Rewrite files in place (implies --apply) and additionally "
            "auto-apply safe import rewrites: flag_private findings "
            "whose target symbol exists on adcp.types, and flag_numbered "
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

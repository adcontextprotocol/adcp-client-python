"""Schema-derived names for union branches (ARCH-ONE-SURFACE §3).

The generator throws a union branch's identity away. ``create-media-buy-response.json``
titles its three branches ``CreateMediaBuySuccess`` / ``CreateMediaBuyError`` /
``CreateMediaBuySubmitted``, and what ships is ``CreateMediaBuyResponse1`` / ``2``
/ ``3``. Verified directly, not assumed, on both candidates: feeding
datamodel-code-generator 0.64.0 a three-branch ``oneOf`` whose branches all carry
``title`` still produces ``DemoResponse1/2/3``, and this repo's own response
emitter numbers its branches by position too. A positional name is the drift channel the whole
one-surface design is about, because inserting an branch upstream silently repoints
an existing public name.

This module answers one question for one schema node — *what is this branch called* —
and nothing else. It does not rename anything and it does not touch the generated
tree, which keeps the naming rule auditable on its own and keeps whoever consumes
it free to apply it wherever the name is actually chosen. Two consumers:

* the custom response emitter in ``scripts/post_generate_fixes.py``, which is
  where the numbers come from for every response union — literally
  ``class_names = [f"{self.base}{index}" for index in range(1, len(branches) + 1)]``.
  Measured: of the schemas with a root union, the generator collapses most into a
  single class and the numbered branches are the emitter's own, so the naming decision
  has exactly one site and it is a line of this repo's code, not the generator's.
* ``main()``, which prints the classification of every root-level branch in the
  pinned bundle — the residual list ARCH-ONE-SURFACE §5 Step 3 asks the pass's
  first run to emit.

A schema transform was measured as the alternative and works on the generator in
isolation: lifting each branch into ``definitions/<DerivedName>`` and leaving a
``$ref`` makes 0.64.0 emit ``DemoSuccess`` / ``DemoError`` / ``DemoSubmitted``
instead of ``DemoResponse1/2/3``. It is not the mechanism used for responses only
because the emitter, not the generator, names those.

**Step zero — classify before naming.** An branch that introduces no property the
base schema does not already declare is a VALIDATION branch: its content is
``required``, ``not``, or a constraint refinement. It must mint no class, because
a public class for it would name something no buyer sends. Only then do the
naming rungs run, in order, first one that holds:

1. the node's ``title``;
2. the discriminator ``const`` — parent + PascalCase(const value);
3. the FULL required set — parent + PascalCase(each sorted required key). Not the
   set-difference against siblings: the difference is empty exactly where one branch
   is the conjunction of two others, and the bundle contains that case
   (``a2ui/bound-value.json`` has five branches requiring ``{literalString}``,
   ``{literalNumber}``, ``{literalBoolean}``, ``{path}``,
   ``{literalString, path}`` — branch 4 overlaps branches 0 and 3, so its difference is
   empty while the full sets are all distinct);
4. fail closed. No counter, no ledger of tolerated numbers: a counter quietly
   becomes load-bearing again.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

#: Branch kinds. ``ref`` already names the shared class it selects; ``scalar`` and
#: ``validation`` mint no class; ``type`` is the population the rungs name.
BranchKind = Literal["ref", "scalar", "validation", "type"]

#: Which rung produced a name, or ``"none"`` for a rung-4 refusal.
Rung = Literal["title", "const", "required-set", "none"]


def pascal_case(value: object) -> str:
    """PascalCase a schema string while PRESERVING interior capitals.

    Distinct from :func:`scripts.generated_names.generated_root_class_name`, which
    is the generator's ROOT naming and lower-cases the tail of a word
    (``Get AdCP Capabilities Request`` -> ``GetAdcpCapabilitiesRequest``). A
    branch title is frequently already written in PascalCase
    (``CreateMediaBuySuccess``), and that rule would mangle it to
    ``Createmediabuysuccess``. Two inputs, two specifications, two functions.

    And this one is NOT a restatement of a generator rule, which is why it stays
    here while the two that were moved to ``scripts/generated_names.py``: the
    generator throws a branch's identity away, so a branch's name is chosen by
    this repo's own emitter and there is no generator answer to ask for.
    """
    words = [word for word in re.split(r"[^0-9a-zA-Z]+", str(value)) if word]
    return "".join(word[:1].upper() + word[1:] for word in words)


def _is_bare_ref(branch: dict[str, Any]) -> bool:
    """A ``$ref`` branch already names the shared class it selects."""
    return "$ref" in branch and not (set(branch) - {"$ref", "title", "description"})


#: Keywords by which an branch says something about a schema's shape.
_STRUCTURAL = frozenset({"properties", "$ref", "allOf", "oneOf", "anyOf"})

#: Keywords by which an branch constrains a shape it does not declare.
_CONSTRAINING = frozenset({"required", "not", "dependencies", "dependentRequired", "if"})


def _declares_a_scalar_value(branch: dict[str, Any]) -> bool:
    """The branch is a value, not an object type: a non-object type, a const, an enum."""
    declared = branch.get("type")
    if isinstance(declared, str) and declared != "object":
        return True
    if isinstance(declared, list) and "object" not in declared:
        return True
    return "const" in branch or "enum" in branch


def _introduced_properties(branch: dict[str, Any], base_properties: dict[str, Any]) -> set[str]:
    own = branch.get("properties")
    if not isinstance(own, dict):
        return set()
    return set(own) - set(base_properties)


def classify(branch: dict[str, Any], base_properties: dict[str, Any]) -> BranchKind:
    """Classify one root-level union branch. See the module docstring's step zero.

    Order matters, and the bundle proves it: ``create-media-buy-request.json``
    anyOf[2] declares only ``required`` and ``not`` — no ``type``, no
    ``properties`` — so a "nothing structural, therefore a scalar" fallback
    claims it, when it is the purest validation branch in the tree. A scalar is
    recognized by what it POSITIVELY declares; a constraining branch is recognized
    by its constraint keywords; only a genuinely empty branch falls through.
    """
    if _is_bare_ref(branch):
        return "ref"
    if _declares_a_scalar_value(branch):
        return "scalar"
    if _introduced_properties(branch, base_properties):
        return "type"
    if set(branch) & (_CONSTRAINING | _STRUCTURAL):
        return "validation"
    return "scalar"


def _discriminator_const(branch: dict[str, Any]) -> object | None:
    """The single pinned value a property of this branch carries, if any."""
    properties = branch.get("properties")
    if not isinstance(properties, dict):
        return None
    for node in properties.values():
        if not isinstance(node, dict):
            continue
        if "const" in node:
            const: object = node["const"]
            return const
        enum = node.get("enum")
        if isinstance(enum, list) and len(enum) == 1:
            single: object = enum[0]
            return single
    return None


def _parent_name(document: dict[str, Any], relative: Path) -> str:
    title = document.get("title")
    if isinstance(title, str) and title.strip():
        return pascal_case(title)
    return pascal_case(relative.stem)


def _candidate(
    document: dict[str, Any],
    branch: dict[str, Any],
    rung: Rung,
    relative: Path,
) -> str | None:
    """One rung's candidate name for one branch, or ``None`` if the rung is silent."""
    if rung == "title":
        title = branch.get("title")
        if isinstance(title, str) and title.strip():
            return pascal_case(title)
        return None
    parent = _parent_name(document, relative)
    if rung == "const":
        const = _discriminator_const(branch)
        return None if const is None else f"{parent}{pascal_case(const)}"
    if rung == "required-set":
        required = branch.get("required")
        if not isinstance(required, list) or not required:
            return None
        keys = sorted(str(key) for key in required)
        return parent + "".join(pascal_case(key) for key in keys)
    return None


#: The rungs, in the order ARCH-ONE-SURFACE §3 sets them.
_RUNGS: tuple[Rung, ...] = ("title", "const", "required-set")


def derive(
    document: dict[str, Any],
    branches: list[Any],
    index: int,
    relative: Path,
) -> tuple[str | None, Rung]:
    """The branch's name by the first rung that holds, or ``(None, "none")``.

    A rung "holds" only when it yields a name that is UNIQUE among the siblings
    at that same rung. The uniqueness test is not an extra safety belt bolted
    onto rung 3 — it is what makes every rung position-free, and the bundle needs
    it on rung 2 as well: ``core/audience-selector.json`` has three branches all
    pinning ``type: "signal"`` (binary, categorical and numeric signals), so the
    const rung names them identically and must fall through to the required set,
    which separates them. Found by the fail-closed rule rather than by reading.
    """
    branch = branches[index]
    siblings = [
        sibling
        for position, sibling in enumerate(branches)
        if position != index and isinstance(sibling, dict) and sibling.get("type") != "null"
    ]
    for rung in _RUNGS:
        mine = _candidate(document, branch, rung, relative)
        if mine is None:
            continue
        if any(_candidate(document, sibling, rung, relative) == mine for sibling in siblings):
            continue
        return mine, rung
    return None, "none"


@dataclass(frozen=True)
class BranchName:
    """One root-level union branch, classified and (if it is a type) named."""

    schema: str
    keyword: str
    index: int
    kind: BranchKind
    name: str | None
    rung: Rung

    @property
    def pointer(self) -> str:
        return f"{self.schema}#/{self.keyword}/{self.index}"


class UnnameableBranchError(RuntimeError):
    """A type branch with no derivable name. Fail closed, naming the schema node."""


def classify_document(document: dict[str, Any], relative: Path) -> list[BranchName]:
    """Classify and name every root-level, non-null union branch of one schema."""
    results: list[BranchName] = []
    base_properties = document.get("properties")
    if not isinstance(base_properties, dict):
        base_properties = {}
    for keyword in ("oneOf", "anyOf"):
        branches = document.get(keyword)
        if not isinstance(branches, list):
            continue
        for index, branch in enumerate(branches):
            if not isinstance(branch, dict) or branch.get("type") == "null":
                continue
            kind = classify(branch, base_properties)
            if kind != "type":
                results.append(BranchName(relative.as_posix(), keyword, index, kind, None, "none"))
                continue
            name, rung = derive(document, branches, index, relative)
            results.append(BranchName(relative.as_posix(), keyword, index, kind, name, rung))
    _refuse_collisions(results, relative)
    return results


def _refuse_collisions(results: list[BranchName], relative: Path) -> None:
    """Two branches of one schema must not derive the same name."""
    taken: dict[str, BranchName] = {}
    for item in results:
        if item.name is None:
            continue
        clash = taken.get(item.name)
        if clash is not None:
            raise UnnameableBranchError(
                f"{relative.as_posix()}: {item.pointer} and {clash.pointer} both derive "
                f"the name {item.name!r}; the rungs must name a schema node uniquely"
            )
        taken[item.name] = item


def schema_files(schema_dir: Path) -> list[Path]:
    """Every schema the pipeline reads: ``bundled/`` and ``mcp/`` are excluded.

    ``bundled/`` is self-contained by design and ``mcp/`` is a per-profile tree
    whose duplicates inflate every count taken over it.
    """
    return [
        path
        for path in sorted(schema_dir.rglob("*.json"))
        if "bundled" not in path.relative_to(schema_dir).parts
        and "mcp" not in path.relative_to(schema_dir).parts
    ]


def classify_bundle(schema_dir: Path) -> list[BranchName]:
    """Classify every root-level union branch in the bundle."""
    results: list[BranchName] = []
    for path in schema_files(schema_dir):
        document = json.loads(path.read_text())
        if not isinstance(document, dict):
            continue
        results.extend(classify_document(document, path.relative_to(schema_dir)))
    return results


def tally(results: list[BranchName]) -> Counter[str]:
    counts: Counter[str] = Counter()
    counts["root branches"] = len(results)
    for item in results:
        if item.kind != "type":
            counts[f"mints no class ({item.kind})"] += 1
        elif item.rung == "none":
            counts["rung 4: FAIL CLOSED"] += 1
        else:
            counts[f"rung: {item.rung}"] += 1
    return counts


def unnameable(results: list[BranchName]) -> list[BranchName]:
    return [item for item in results if item.kind == "type" and item.name is None]


def main(argv: list[str] | None = None) -> int:
    """Print the classification of the pinned bundle's root-level union branches."""
    argv = list(sys.argv[1:] if argv is None else argv)
    schema_dir = Path(argv[0]) if argv else Path("schemas/cache/3.2")
    results = classify_bundle(schema_dir)
    counts = tally(results)
    print(f"bundle: {schema_dir} ({len(schema_files(schema_dir))} schema files)")
    for key in sorted(counts):
        print(f"  {counts[key]:5d}  {key}")
    refused = unnameable(results)
    print(f"\nunnameable type branches: {len(refused)}")
    for item in refused:
        print(f"  {item.pointer}")
    derived = [item for item in results if item.name]
    print(f"\nnames derived (first 40 of {len(derived)}):")
    for item in derived[:40]:
        print(f"  {item.rung:13s} {item.pointer:68s} -> {item.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# --------------------------------------------------------------------------- #
# Branch classes: the same rungs, over the records the constraint reader built
# --------------------------------------------------------------------------- #
#
# Two amendments the branch-class design forces, both mechanical.
#
# STEP ZERO INVERTS. The rule above says an branch introducing no new property is a
# validation branch and must mint no class -- correct while a flat root class exists
# for it to refine. Where the branch-class emitter runs, that class is gone: the
# branch IS the request shape, so on a flat-root combinator schema whose branches
# partition, every branch mints a class. ``classify`` is therefore not consulted on
# that path.
#
# THE TITLE RUNG TAKES THE PARENT PREFIX TOO. A response branch's title already
# carries its stem (``CreateMediaBuySuccess``), but a request branch's title is
# prose: a bare ``CommittedProposalExecution`` on the flat surface does not say
# what it is. Parent + PascalCase(title) is as mechanical as parent + const, and
# a title that already starts with the parent name is not doubled.


def _record_candidate(parent: str, branch: Any, rung: Rung) -> str | None:
    """One rung's candidate name for one ``Branch`` record."""
    if rung == "title":
        if not branch.title or not str(branch.title).strip():
            return None
        return _prefixed(parent, pascal_case(branch.title))
    if rung == "const":
        if branch.discriminator is None:
            return None
        return f"{parent}{pascal_case(branch.discriminator[1])}"
    if rung == "required-set":
        if not branch.required:
            return None
        return parent + "".join(pascal_case(key) for key in sorted(branch.required))
    return None


#: Splits a PascalCase identifier into its words, acronyms kept whole.
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")

#: How many leading words a title must share with the parent to be read as
#: already carrying its stem. Two, because one is a coincidence a prose title
#: hits often ("Create" against CreateMediaBuyRequest) and three would miss
#: the two-word stems the bundle writes.
_STEM_WORDS = 2


def _prefixed(parent: str, derived: str) -> str:
    """``parent + derived``, unless the title already carries the parent's stem.

    A response branch's title is written with its stem -- ``CreateMediaBuySuccess``
    under ``CreateMediaBuyResponse`` -- and prefixing it again spells the stem
    twice. A request branch's title is prose (``Committed proposal execution``) and
    says nothing about which request it is, so it takes the prefix. The test is
    the two names' shared leading WORDS, which needs no list of suffixes to
    strip and no table of stems: ``Create Media Buy`` is shared, ``Committed``
    shares nothing.
    """
    parent_words = _WORDS.findall(parent)
    derived_words = _WORDS.findall(derived)
    shared = 0
    for mine, theirs in zip(parent_words, derived_words):
        if mine != theirs:
            break
        shared += 1
    return derived if shared >= _STEM_WORDS else f"{parent}{derived}"


def branch_class_names(parent: str, branches: Sequence[Any], schema: str) -> tuple[str, ...]:
    """One class name per branch, from the branch's own node identity.

    ``branches`` are ``scripts.schema_constraints.Branch`` records, so this consults no
    schema node of its own -- the reader already read them, and a second reading
    is the drift channel the one-reader rule exists to close.

    The first rung that holds for an branch names it, and a rung "holds" only when
    its candidate is unique among the siblings at the same rung: a shared title
    or a shared const names nothing, and must fall through to the required set.
    An branch no rung names raises :class:`UnnameableBranchError` rather than taking a
    position -- there is no counter and no ledger of tolerated numbers.
    """
    names: list[str] = []
    for index, branch in enumerate(branches):
        siblings = [other for position, other in enumerate(branches) if position != index]
        chosen: str | None = None
        for rung in _RUNGS:
            candidate = _record_candidate(parent, branch, rung)
            if candidate is None:
                continue
            if any(_record_candidate(parent, other, rung) == candidate for other in siblings):
                continue
            chosen = candidate
            break
        if chosen is None:
            raise UnnameableBranchError(
                f"{schema}#/{branch.pointer}: no rung names this branch -- it carries no "
                f"title, no discriminating const and no required set unique among its "
                f"siblings. A class named by its position is the drift channel this "
                f"rule exists to close, so generation refuses instead."
            )
        names.append(chosen)
    _refuse_record_collisions(parent, branches, names, schema)
    return tuple(names)


def _refuse_record_collisions(
    parent: str, branches: Sequence[Any], names: Sequence[str], schema: str
) -> None:
    taken: dict[str, str] = {}
    for branch, name in zip(branches, names, strict=True):
        clash = taken.get(name)
        if clash is not None:
            raise UnnameableBranchError(
                f"{schema}: {branch.pointer} and {clash} both derive the name {name!r}; "
                f"the rungs must name a schema node uniquely"
            )
        taken[name] = branch.pointer

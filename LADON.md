# Ladon configuration

## Repo Context

`adcontextprotocol/adcp-client-python` is the official Python SDK for the Ad
Context Protocol, published to PyPI as `adcp`. Stack: Python 3.10+, Pydantic v2
models generated from the canonical AdCP JSON Schemas, released via
release-please + conventional commits. The public surface is everything reachable
from `src/adcp/__init__.py` and its re-export tree (`adcp.types`, `adcp.server`,
`adcp.decisioning`, `adcp.client`, `adcp.protocols`, `adcp.testing`). Reviews
weigh wire-shape fidelity, public-API stability, and the semver signal on the
commit above style. The three local gates are the floor: `ruff check src/`,
`mypy src/adcp/`, `pytest tests/`.

### Mandatory: semver signal on the public surface

When the diff removes, renames, or changes the type signature of a public export
from the `adcp.*` namespace; flips a required Pydantic field to optional or back
on a public model; removes an enum member from a public enum; or changes a
response model's shape so existing buyers/sellers stop deserializing — the commit
MUST carry `feat!:` / `fix!:` or a `BREAKING CHANGE:` footer, plus a migration
note (PR body, a `MIGRATION_*.md`, or `CHANGELOG.md` prose). A breaking diff
shipped under a non-breaking conventional-commit prefix is a `high` finding:
release-please cuts a minor from `feat:`, so the break ships without a major.

### Mandatory: forward-compat on discriminated unions

`src/adcp/types/_forward_compat.py`, `aliases.py`, and `_ergonomic.py` are
load-bearing. Removing an `UnknownFormatAsset`-style fallback arm, narrowing
`additionalProperties` on a published variant, removing a discriminator value
without an open-union escape hatch, or changing the discriminator key on a model
already on the wire is a `high` finding — adopter deserialization breaks the
moment upstream adds a variant.

### Mandatory: type-system import layering

Only `_generated.py`, `_eager.py`, `aliases.py`, `capabilities.py`,
`_ergonomic.py`, `_forward_compat.py`, and `types/__init__.py` may import from
`src/adcp/types/generated_poc/**` or `src/adcp/types/_generated.py`;
`tests/test_import_layering.py` enforces the allowlist. Any other module
importing generated names directly is a `high` finding — it ships unstable
codegen identifiers to adopters. `adcp/types/__init__.py` is a PEP 562 lazy
facade with its runtime `__getattr__`/`__dir__` under `if not TYPE_CHECKING:`;
reintroducing eager imports there, or moving the `__getattr__` out from under the
guard, regresses both import cost and type-checker coverage.

### Mandatory: generated code is not source

A hand-edit inside `src/adcp/types/generated_poc/**` or
`src/adcp/types/_generated.py` is a `high` finding. Legitimate changes there are
regeneration output and pair with an upstream schema change under `schemas/` or an
`src/adcp/ADCP_VERSION` bump; check that `aliases.py` and `_ergonomic.py` still
cover the regenerated names. `datamodel-code-generator` numbers anonymous variant
classes by traversal order, so a diff consisting only of `ClassNameN →
ClassNameM` renames is codegen churn, not a schema delta — say so rather than
reviewing it as a semantic change, and flag that accepting the renumber breaks
the `aliases.py` layer for nothing.

### Mandatory: no credentials in `ctx_metadata`

`RequestContext.metadata` is echoed back into responses per the AdCP context-echo
contract and lands in the idempotency replay cache. Storing a credential there is
a `critical` finding. Credentials belong in `AuthInfo.credential` or the typed
classes in `adcp.decisioning` (`ApiKeyCredential`, `OAuthCredential`,
`HttpSigCredential`). `_build_request_context` fail-closes on credential-shaped
keys; weakening or removing `_CREDENTIAL_SHAPED_KEY_SUFFIXES` is the same finding.

### Mandatory: CI gates stay armed

Disabling a test instead of fixing it, dropping a `ruff` rule without a stated
reason, or silencing mypy with a blanket `# type: ignore` is a `high` finding.
Specific codes (`# type: ignore[no-any-return]`) are fine — the blanket form is
the problem.

### Docs here are adopter-facing behavior

`skills/*/SKILL.md`, `AGENTS.md`, `llms.txt`, and `README.md` document the build
paths adopters and coding agents follow. A multi-hundred-line `SKILL.md` is a
behavior-affecting change, not a docs tweak; the largest-file rule applies. A new
public export landing without a matching entry in `AGENTS.md`'s handler table is a
`medium` finding.

## High-Risk Paths

- src/adcp/__init__.py
- src/adcp/decisioning/**
- src/adcp/server/**
- src/adcp/protocols/**
- src/adcp/signing/**
- src/adcp/webhook_auth.py
- src/adcp/_idempotency.py
- src/adcp/migrate/**
- src/adcp/compat/**
- src/adcp/types/__init__.py
- src/adcp/types/_eager.py
- src/adcp/types/_ergonomic.py
- src/adcp/types/_forward_compat.py
- src/adcp/types/aliases.py
- src/adcp/types/mypy_plugin.py
- src/adcp/ADCP_VERSION
- schemas/**

## Escalation Reviewers

- bokelley

## Trivial Paths

- src/adcp/types/generated_poc/**
- src/adcp/types/_generated.py
- CHANGELOG.md

## Release Stack Branches

- release-please--branches--main--components--adcp

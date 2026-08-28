# Media-buy action rights

AdCP exposes three related action surfaces with different authority:

1. `Product.allowed_actions` says what a product may support. It is advisory.
2. `Proposal.commercial_terms.change_terms` records the rights accepted in a deal.
3. `MediaBuy.available_actions` says which accepted rights are executable now.

The SDK joins those surfaces without promoting product templates or legacy
compatibility fields into authority.

## Buyer assessment

```python
from adcp import assess_media_buy_action

assessment = assess_media_buy_action(
    "increase_budget",
    product=product,
    proposal=accepted_proposal,
    media_buy=current_media_buy,
    intent={
        "current_amount": "1000",
        "result_amount": "1100",
        "currency": "USD",
    },
)

if assessment.status == "available_now":
    print(assessment.task, assessment.mode)
else:
    print(assessment.possible, assessment.promised, assessment.available)
```

The status is one of `available_now`, `wrong_status`, `not_negotiated`,
`unsupported_by_product`, `currently_unavailable`, or `legacy_unknown`.
Portable budget, flight, package-count, and effective-time bounds are checked
when the caller supplies enough current/result state. Opaque condition IDs and
contract references are never executed or interpreted by the SDK.

For deprecated `update_media_buy` patches,
`assess_update_media_buy_actions()` first decomposes the patch into canonical
actions and then applies the same checks. Fine-grained beta.9 actions retain
coarse 3.x candidates so older seller declarations remain readable without
expanding authority.

## Routing and races

`route_media_buy_action()` selects the normal compact task. Operational
controls use `control_media_buy`, commercial amendments use
`refine_proposals`, and creative mutations use `sync_creatives`. Some actions
are valid through either control or refinement; an authoritative live action's
explicit `task` wins when the protocol permits it.

`dispatch_media_buy_action()` is asynchronous and accepts an already validated
assessment plus the generated request model for that task. A
`seller_managed` action uses the ordinary asynchronous task lifecycle; it does
not introduce a seller-review MediaBuy status.

Always send the latest MediaBuy `revision` and an idempotency key. If an
`ACTION_NOT_ALLOWED` race returns `currently_available_actions`, pass that echo
to `reassess_media_buy_action()` for an immediate explanation, then refresh the
full MediaBuy before retrying with its new revision.

## Seller materialization and projection

```python
from adcp import ChangeTermSelection, materialize_change_terms

change_terms = materialize_change_terms(
    product.allowed_actions,
    [
        ChangeTermSelection(
            action="increase_budget",
            term_id="right_budget_1",
            service_mode="seller_managed",
            allowed_statuses=("active",),
        )
    ],
)
```

Only explicit selections become binding terms. The builder rejects duplicate
actions and term IDs, expanded status scopes, unadvertised modes, and
action/constraint mismatches.

Use `project_available_actions()` to derive the current surface from accepted
terms. Optional authorization, delegation, seller-policy, product, and
resolved-condition gates only narrow the result. Conditions must be explicitly
resolved to `True`; missing or indeterminate condition state fails closed.

## Version behavior

| Wire version | Projection |
|---|---|
| AdCP 3.1.19 | `terms_ref` compatibility alias; no inferred proposal identity |
| Early AdCP 3.2 beta | explicit `task`, legacy `terms_ref`, `requires_approval` compatibility mode |
| AdCP 3.2 beta.9+ | explicit `task`, `seller_managed`, and `change_term_id` |

An arbitrary inbound 3.1 `terms_ref` remains opaque even when its text matches
a proposal term ID. When a 3.2 payload contains both aliases, unequal values
fail closed.

The language-neutral fixture at
`tests/fixtures/media_buy_action_assessment.json` defines normalized buyer
results for cross-SDK parity.

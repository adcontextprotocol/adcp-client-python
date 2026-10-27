# Media-buy state machine

`adcp.decisioning` publishes the AdCP media-buy lifecycle as data, so a
seller does not keep its own copy of the graph. There are two graphs,
because a seller changes a buy's status in two different ways:

| | Graph | Use it for |
|---|---|---|
| **Requested** | `MEDIA_BUY_TRANSITIONS` | A handler changing status on a buyer's request (`update_media_buy`, `control_media_buy`). `assert_media_buy_transition` refuses an illegal move with `INVALID_STATE`. |
| **Observed** | `OBSERVED_MEDIA_BUY_TRANSITIONS` | Recording a status the seller's own platform (ad server, OMS) reports. `is_observable_media_buy_transition` answers whether the move is on the published graph. |

The observed graph is a strict superset of the requested one. It adds the
three AdCP 3.2 edges that follow from the seller's lifecycle rather than from a
status a buyer asks for:

| Observed edge | AdCP 3.2 source |
|---|---|
| `pending_creatives → active` | Creative coverage completed after the flight started. [Lifecycle, "Creative sync timing"](https://docs.adcontextprotocol.org/docs/media-buy/media-buys/lifecycle#creative-sync-timing): the buy "moves to `pending_start` when the flight is in the future or `active` when delivery can begin." |
| `pending_creatives → paused` | Same, on a buy created with `paused: true`. Lifecycle transition diagram, "sync_creatives assignment (buy held)". |
| `pending_start → paused` | The flight started on a buy created with `paused: true`. Lifecycle transition diagram, "flight date reached (create held)"; the [media-buy specification](https://docs.adcontextprotocol.org/docs/media-buy/specification#media-buy-state-transitions) says the hold is latent while setup blockers are visible and "once creatives are present and the flight can start, the buy enters `paused` instead of `active`." |

It is the same graph the `@adcp/client` storyboard runner's `status.monotonic`
invariant enforces for AdCP 3.2, so a status history this graph accepts also
passes that invariant.

## Terminal statuses

`MEDIA_BUY_TERMINAL_STATUSES` is `{"completed", "canceled", "rejected"}`. It is
derived from the graph, not listed by hand. `is_terminal_media_buy_status`
returns `False` for a status the spec does not define, so an internal
pre-lifecycle status such as `draft` or `pending_approval` is simply
non-terminal.

```python
from adcp.decisioning import is_terminal_media_buy_status

if is_terminal_media_buy_status(stored.status):
    return  # nothing more to sync from the ad server
```

The creative graph has no terminal status: a rejected creative returns to
`processing` when resubmitted, and an archived one can be unarchived. There is
no `CREATIVE_TERMINAL_STATUSES`.

## Mirroring an ad server

```python
from adcp.decisioning import is_observable_media_buy_transition

def record_platform_status(buy, reported: str) -> None:
    if reported == buy.status:
        return
    if is_observable_media_buy_transition(buy.status, reported):
        buy.status = reported
        return
    # Not on the AdCP 3.2 graph: keep the reported status, log the platform state.
    log.warning("ad server moved %s %s -> %s", buy.id, buy.status, reported)
```

`is_observable_media_buy_transition` returns `False` for an unchanged status,
for an unknown status, and for any move out of a terminal status.

## Platform moves AdCP 3.2 does not define

Some ad-server moves have no edge in AdCP 3.2, and the SDK does not add one:

- `completed → active` or `completed → paused`, when an order's end date is
  extended or a finished order is reopened. The specification says terminal
  states "allow no further transitions".
- `active → pending_start`, when the flight start is moved later. The graph has
  no edge back to an earlier phase.

A seller that sees one of these should keep the status it reports to buyers on
the published graph, and track the platform's state separately, rather than
send a history that `status.monotonic` rejects.

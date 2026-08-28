# Seller acceptance-policy discovery

AdCP 3.2 sellers can advertise an `acceptance_policy_discovery` catalog in
their media-buy capabilities. Products can add
`acceptance_policy_profile_ids`. The Python SDK can resolve those profiles and
produce a conservative advisory assessment before a buyer sends a task.

Discovery never replaces the seller's task response. An `allowed` assessment
means only that the verified, published profiles allow the contemplated action.
An absent catalog, a partial profile, an unavailable registry policy, a missing
buyer fact, or an invalid digest produces `unknown` rather than permission.

```python
from adcp import AcceptancePolicyOutcome, AcceptancePolicyResolver

discovery = capabilities.media_buy.acceptance_policy_discovery
product_profiles = product.acceptance_policy_profile_ids or []

async with AcceptancePolicyResolver() as resolver:
    assessment = await resolver.assess(
        discovery,
        {
            "subjects": [
                {
                    "subject_category": "political",
                    "subject_facets": ["candidate_election"],
                }
            ],
            "advertiser_roles": ["political_actor"],
            "delivery_jurisdictions": ["US"],
        },
        applies_to="media_buy",
        product_profile_ids=product_profiles,
        cache_ttl_seconds=capabilities.capability_changes.cache_ttl_seconds,
        capabilities_version=capabilities.capability_changes.capabilities_version,
    )

if assessment.outcome is AcceptancePolicyOutcome.prohibited:
    # Do not submit this configuration.
    ...
elif assessment.outcome is AcceptancePolicyOutcome.unknown:
    # Ask the seller or submit the exact task and handle its authoritative result.
    ...
```

The other outcomes identify the coarse next step:

- `requires_disclosure`: add a declaration, disclosure, funding statement, or
  transparency information described by the typed requirements.
- `requires_setup`: complete advertiser verification, eligibility, account
  setup, licensing, or certification.
- `requires_review`: obtain authorization or seller review, or satisfy a typed
  targeting, creative, destination, format, or time restriction.

Do not execute the free-text `description` fields in catalogs, profiles, rules,
or policies. They are display-only. Use typed rule dimensions and requirements;
the exact obligations remain in the digest-pinned registry policies.

## Cache and invalidation

Pass only the TTL advertised by `capability_changes.cache_ttl_seconds`. The
resolver keys cached catalogs by canonical URL and exact digest, bounds the
cache, and never caches failures. Call `invalidate_capabilities()` after a
`capabilities.changed` notification, then re-read seller capabilities before
assessing again.

Catalog requests use HTTPS with no caller credentials, no redirects, an
IP-pinned public-address transport, a five-second default timeout, and a 1 MiB
decoded-body limit. The catalog digest is checked against the exact decoded
representation bytes before JSON parsing or schema validation. Local profile
digests and registry policy content use RFC 8785 JCS as specified by AdCP.

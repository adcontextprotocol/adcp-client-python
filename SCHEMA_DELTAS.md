# Generated-types delta

## Field changes

- `adagents.py`
  - **classes removed**: Reason, RevokedPublisherDomain
  - `AdcpAgentsAuthorization2`: `-revoked_publisher_domains`
- `core/publisher_property_selector.py`
  - **classes removed**: PublisherDomain
  - `PublisherPropertySelector1`: `-publisher_domains`
  - `PublisherPropertySelector3`: `-publisher_domains`
- `tmp/identity_match_request.py`
  - `IdentityMatchRequest`: `+seller_agent_url`

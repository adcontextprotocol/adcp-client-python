# Publisher agent discovery

Agent lookup treats host case and explicit default ports as equivalent, and
continues to match HTTP and HTTPS URLs. Paths remain case-sensitive; trailing
slashes, query strings and fragments do not affect matching. Non-default ports
remain significant.

Use the listing helper alongside property resolution to distinguish an agent
that is listed but whose selectors match no properties from an unlisted agent:

```python
from adcp import find_authorized_agent_entries, get_properties_by_agent

entries = find_authorized_agent_entries(document, "https://sales.example.com/mcp")
properties = get_properties_by_agent(document, "https://sales.example.com/mcp")
listed_but_unbound = bool(entries) and not properties
```

Malformed sibling entries and non-string or empty URL values are skipped during
lookup. `validate_adagents_structure(document)` reports invalid URL types through
its existing `missing_url` diagnostic. The listing helper returns matching entry
objects in source order and does not modify the document.

# ADCP Protocol Improvement Suggestions

This document tracks suggestions for improvements to the ADCP specification based on real-world implementation experience with the Python client.

## 1. Batch Preview Generation (`preview_creatives`)

### Problem

Currently, generating previews for multiple formats requires N separate API calls:

```python
# Current approach - N API calls
for format_id in product.format_ids:  # e.g., 5 formats
    preview = await client.preview_creative(
        format_id=format_id,
        manifest=manifest
    )
    # Result: 5 × 200ms = 1 second
```

Even with parallel requests via `asyncio.gather()`, this is inefficient:
- Multiple HTTP round trips
- Repeated connection overhead
- Higher latency for users
- More load on creative agents

### Proposed Solution

Add a batch endpoint that accepts multiple preview requests:

```python
# Proposed batch API - 1 API call
previews = await client.preview_creatives(
    requests=[
        PreviewCreativeRequest(format_id="display_300x250", manifest=manifest1),
        PreviewCreativeRequest(format_id="display_728x90", manifest=manifest2),
        PreviewCreativeRequest(format_id="video_30s", manifest=manifest3),
    ]
)
# Result: 1 call = ~200ms total
```

### Specification

**Task Name:** `preview_creatives` (plural)

**Request:**
```typescript
{
  requests: PreviewCreativeRequest[]  // Array of preview requests
}
```

**Response:**
```typescript
{
  previews: PreviewCreativeResponse[]  // Array of responses, same order as requests
  errors?: Array<{
    index: number           // Index of failed request
    error: string          // Error message
  }>
}
```

### Benefits

1. **Performance**: 5-10x faster for typical use cases
2. **Efficiency**: Single HTTP connection, one round trip
3. **Resource Usage**: Less overhead on creative agents
4. **Better UX**: Faster grid rendering for buyers
5. **Scalability**: Easier to handle high-concurrency scenarios

### Implementation Notes

**For Creative Agents:**
- Can process requests in parallel internally
- Can optimize asset loading (deduplicate common assets)
- Can share rendering context across formats
- Backward compatible (keep `preview_creative` for single requests)

**For Clients:**
- Fallback to individual requests if batch not supported
- Can use feature detection or version negotiation
- Batch size limits configurable per agent

### Priority

**High** - This is a common pattern in production usage:
- Product catalogs showing multiple format previews
- Format browsers displaying grid of all formats
- Media buyers comparing different format options
- Creative QA tools testing multiple variations

### Example Use Cases

**1. Product Grid Display**
```python
# Buyer views catalog with 20 products, each supporting 3 formats
# Current: 60 API calls
# With batch: 1-3 API calls (batch by product or all at once)
```

**2. Format Browser**
```python
# Buyer browses creative formats (50 total)
# Current: 50 API calls
# With batch: 1 API call
```

**3. Creative QA**
```python
# QA team validates creative across 10 formats
# Current: 10 API calls
# With batch: 1 API call
```

### Migration Path

1. **Phase 1**: Add `preview_creatives` to spec (v2.4?)
2. **Phase 2**: Reference creative agent implements it
3. **Phase 3**: Client libraries add support with fallback
4. **Phase 4**: Deprecate single `preview_creative` (optional, maybe v3.0?)

### Related Patterns

This follows established patterns in other protocols:
- GraphQL: Batching multiple queries
- gRPC: Streaming RPCs
- REST: Batch endpoints (e.g., `/batch`)
- MCP: Multiple tool calls in one request

## 2. Structured Format Listing via MCP

### Problem

The reference creative agent at `https://creative.adcontextprotocol.org` returns text messages via MCP instead of structured data:

```python
result = await client.list_creative_formats()
# Returns: [TextContent(text="Found 42 creative formats")]
# Expected: ListCreativeFormatsResponse(formats=[...])
```

### Impact

- Can't test `fetch_previews` feature with real creative agent
- Forces use of mock data in examples
- Inconsistent with A2A protocol behavior

### Suggested Fix

MCP tool responses should return structured JSON in the text field:

```python
# Current
TextContent(text="Found 42 creative formats")

# Should be
TextContent(text='{"formats": [...], "errors": null}')
```

This aligns with how A2A returns data and allows proper parsing.

---

## How to Provide Feedback

These suggestions are based on implementation experience with the Python client. To discuss:

1. **GitHub**: Open an issue at [adcp-spec repository]
2. **Email**: Contact the ADCP working group
3. **Community**: Discuss in ADCP Slack/Discord

Feedback from other implementers (JavaScript, Java, etc.) would be valuable to validate these patterns across languages.

# ADCP Protocol Improvement Suggestions

This document tracks suggestions for improvements to the ADCP specification based on real-world implementation experience with the Python client.

## Status Update

**✅ PR #183 - Batch Preview & HTML Output** - Currently open at https://github.com/adcontextprotocol/adcp/pull/183

This PR implements both suggestions below (batch API + HTML output format) with comprehensive documentation. Once merged and schemas published, we'll update this Python client to support the new features.

### Integration Timeline

1. **PR Merge** - Waiting for PR #183 to merge
2. **Schema Publication** - adcontextprotocol.org schemas updated
3. **Client Update** - Regenerate Pydantic models and implement batch support
4. **Testing** - Validate against reference creative agent
5. **Release** - Publish new Python client version with batch support

---

## 1. Batch Preview Generation (`preview_creatives`)

**✅ IMPLEMENTED IN PR #183** - See https://github.com/adcontextprotocol/adcp/pull/183

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

## 2. HTML Output Format for Direct Embedding

**✅ IMPLEMENTED IN PR #183** - See https://github.com/adcontextprotocol/adcp/pull/183

PR #183 adds an `output_format` parameter to `preview_creative`:
- `output_format: "url"` (default) - Returns preview URLs for iframe embedding
- `output_format: "html"` - Returns raw HTML for direct embedding

This eliminates 50+ iframe HTTP requests in preview grids!

### Python Client Usage (once PR #183 merges):

```python
# Get HTML output for direct embedding
result = await client.list_creative_formats(
    request,
    fetch_previews=True,
    preview_output_format="html"  # ← New parameter!
)

formats_with_previews = result.metadata["formats_with_previews"]
for fmt in formats_with_previews:
    preview_html = fmt["preview_data"]["preview_html"]
    # Embed directly - no iframe HTTP request needed!
```

**Security Note:** HTML output should only be used with trusted creative agents since it bypasses iframe sandboxing.

---

## 3. Structured Format Listing via MCP

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

## Python Client Implementation Plan for PR #183

Once PR #183 merges and schemas are published, here's what we'll implement:

### Phase 1: Schema & Types (Day 1)

1. **Sync schemas**: Run `python scripts/sync_schemas.py` to get updated schemas
2. **Regenerate models**: Run `python scripts/generate_models_simple.py`
3. **Verify types**: Check that `PreviewCreativeRequest` supports both single and batch modes
4. **Update imports**: Add new types to `src/adcp/types/tasks.py`

### Phase 2: Batch Mode Support (Days 2-3)

1. **Update `PreviewURLGenerator`**:
   ```python
   async def get_preview_data_batch(
       self,
       requests: list[tuple[FormatId, CreativeManifest]]
   ) -> list[dict[str, Any] | None]:
       """Generate preview data for multiple manifests in one API call."""
       # Build batch request
       batch_request = PreviewCreativeRequest(
           requests=[
               {"format_id": fid, "creative_manifest": manifest}
               for fid, manifest in requests
           ]
       )
       result = await self.creative_agent_client.preview_creative(batch_request)
       # Parse batch response
       return [item.response if item.success else None for item in result.data.results]
   ```

2. **Update `add_preview_urls_to_formats`**:
   ```python
   async def add_preview_urls_to_formats(
       formats: list[Format], creative_agent_client: ADCPClient, use_batch: bool = True
   ) -> list[dict[str, Any]]:
       generator = PreviewURLGenerator(creative_agent_client)

       if use_batch and len(formats) > 1:
           # Use batch API (5-10x faster!)
           manifests = [_create_sample_manifest_for_format(f) for f in formats]
           preview_data_list = await generator.get_preview_data_batch(
               [(f.format_id, m) for f, m in zip(formats, manifests) if m]
           )
           # Process results...
       else:
           # Fall back to individual requests
           # (existing code)
   ```

3. **Add batch size limits**: Respect 50-item limit from spec

### Phase 3: HTML Output Format (Days 4-5)

1. **Add `output_format` parameter**:
   ```python
   async def get_products(
       self,
       request: GetProductsRequest,
       fetch_previews: bool = False,
       preview_output_format: Literal["url", "html"] = "url",  # ← New!
       creative_agent_client: ADCPClient | None = None,
   ) -> TaskResult[GetProductsResponse]:
   ```

2. **Pass through to preview generator**:
   ```python
   preview_data = await generator.get_preview_data_for_manifest(
       format_id,
       manifest,
       output_format=preview_output_format  # ← Pass through
   )
   ```

3. **Update response handling**:
   ```python
   if output_format == "html":
       preview_data["preview_html"] = render.preview_html
   else:
       preview_data["preview_url"] = render.preview_url
   ```

### Phase 4: Testing (Days 6-7)

1. **Unit tests**:
   - Test batch mode with 1, 10, 50 items
   - Test batch mode with partial failures
   - Test HTML vs URL output
   - Test array ordering guarantee

2. **Integration tests**:
   - Test against reference creative agent
   - Verify batch performance improvements
   - Validate HTML embedding safety

3. **Examples**:
   - Update `examples/fetch_preview_urls.py` to use batch mode
   - Add `examples/html_embedding_demo.html` showing direct embedding
   - Add performance comparison examples

### Phase 5: Documentation (Day 8)

1. **Update README.md**: Document batch mode and HTML output
2. **Update CHANGELOG.md**: Note breaking changes (if any)
3. **Migration guide**: Show before/after for existing users
4. **Security guide**: Document HTML embedding risks

### Expected Performance Improvements

- **Format catalog (50 formats)**: 10.5s → 1.2s (8.75x faster)
- **Product grid (20 products × 3 formats)**: 12s → 1.5s (8x faster)
- **With HTML output**: Additional 2-3x improvement (no iframe requests)
- **Combined**: Up to 25x performance improvement!

---

## How to Provide Feedback

These suggestions are based on implementation experience with the Python client. To discuss:

1. **GitHub**: Open an issue at [adcp-spec repository]
2. **Email**: Contact the ADCP working group
3. **Community**: Discuss in ADCP Slack/Discord

Feedback from other implementers (JavaScript, Java, etc.) would be valuable to validate these patterns across languages.

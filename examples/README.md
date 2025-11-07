# ADCP Python Client Examples

This directory contains examples demonstrating how to use the ADCP Python client's preview URL generation feature.

## Preview URL Generation Demo

This demo shows how to fetch creative format previews from the ADCP creative agent and display them in a web browser using the `<rendered-creative>` web component.

### Quick Start

1. **Install the package** (if not already installed):
   ```bash
   pip install -e .
   ```

2. **Fetch preview URLs from the creative agent**:
   ```bash
   python examples/fetch_previews.py
   ```

   This will:
   - Connect to the reference creative agent at `https://creative.adcontextprotocol.org`
   - Call `list_creative_formats()` with `fetch_previews=True`
   - Generate preview URLs for available formats
   - Save the results to `examples/preview_urls.json`

3. **Start a local web server** (required for the web component to work):
   ```bash
   cd /path/to/adcp-client-python
   python -m http.server 8000
   ```

4. **Open the demo in your browser**:
   ```
   http://localhost:8000/examples/web_component_demo.html
   ```

### What You'll See

The demo page displays a grid of creative format previews, each showing:
- Format name
- Format ID
- Live preview rendered in the `<rendered-creative>` web component

Features demonstrated:
- **Shadow DOM isolation** - No CSS conflicts between previews
- **Lazy loading** - Previews load only when visible
- **Responsive grid** - Adapts to different screen sizes
- **Interactive previews** - Full creative rendering with animations

### Files

- **`fetch_previews.py`** - Python script to fetch preview URLs from the creative agent
- **`web_component_demo.html`** - HTML page demonstrating the `<rendered-creative>` web component
- **`preview_urls.json`** - Generated file containing preview URLs (created by fetch script)

### How It Works

The Python script uses the new `fetch_previews` parameter:

```python
from adcp import ADCPClient
from adcp.types import AgentConfig, Protocol
from adcp.types.generated import ListCreativeFormatsRequest

creative_agent = ADCPClient(
    AgentConfig(
        id="creative_agent",
        agent_uri="https://creative.adcontextprotocol.org",
        protocol=Protocol.MCP,
    )
)

# Fetch formats with preview URLs
result = await creative_agent.list_creative_formats(
    ListCreativeFormatsRequest(),
    fetch_previews=True  # ← New parameter!
)

# Access preview data
formats_with_previews = result.metadata["formats_with_previews"]
for fmt in formats_with_previews:
    preview_data = fmt["preview_data"]
    print(f"Preview URL: {preview_data['preview_url']}")
    print(f"Expires: {preview_data['expires_at']}")
```

The HTML page then uses the official ADCP web component to render the previews:

```html
<!-- Include the web component -->
<script src="https://creative.adcontextprotocol.org/static/rendered-creative.js"></script>

<!-- Render previews -->
<rendered-creative
    src="{{preview_url}}"
    width="300"
    height="400"
    lazy="true">
</rendered-creative>
```

### Troubleshooting

**"Preview URLs file not found" error:**
- Run `python examples/fetch_previews.py` first

**Web component not loading:**
- Make sure you're using a web server (not `file://` URLs)
- Check that you have internet access (web component loads from CDN)

**No previews showing:**
- Check browser console for errors
- Verify the creative agent is accessible: `https://creative.adcontextprotocol.org`

### Using with Products

You can also fetch preview URLs for products:

```python
from adcp.types.generated import GetProductsRequest

# Setup publisher and creative agents
publisher_agent = ADCPClient(publisher_config)
creative_agent = ADCPClient(creative_config)

# Get products with preview URLs
result = await publisher_agent.get_products(
    GetProductsRequest(brief="video campaign"),
    fetch_previews=True,
    creative_agent_client=creative_agent
)

# Access product previews
products_with_previews = result.metadata["products_with_previews"]
for product in products_with_previews:
    print(f"Product: {product['name']}")
    for format_id, preview_data in product["format_previews"].items():
        print(f"  Format {format_id}: {preview_data['preview_url']}")
```

## More Examples

For more examples, see the [documentation](https://docs.adcontextprotocol.org) and [integration tests](../tests/integration/).

"""
Example script to fetch preview URLs from the ADCP creative agent.

This demonstrates the new preview URL generation feature by:
1. Connecting to the reference creative agent
2. Listing available formats
3. Generating preview URLs for each format
4. Saving the results to use in the web component demo
"""

import asyncio
import json
from pathlib import Path

from adcp import ADCPClient
from adcp.types import AgentConfig, Protocol
from adcp.types.generated import ListCreativeFormatsRequest


async def main():
    """Fetch preview URLs from the creative agent."""

    # Connect to the reference creative agent
    creative_agent = ADCPClient(
        AgentConfig(
            id="creative_agent",
            agent_uri="https://creative.adcontextprotocol.org",
            protocol=Protocol.MCP,
        )
    )

    print("Fetching creative formats with preview URLs...")
    print("=" * 60)

    try:
        # List formats with preview URL generation
        result = await creative_agent.list_creative_formats(ListCreativeFormatsRequest(), fetch_previews=True)

        if not result.success:
            print(f"❌ Failed to fetch formats: {result.error}")
            return

        # Get formats with previews from metadata
        formats_with_previews = result.metadata.get("formats_with_previews", [])

        print(f"\n✅ Successfully fetched {len(formats_with_previews)} formats with previews\n")

        # Display preview URLs
        preview_data = []
        for fmt in formats_with_previews[:5]:  # Show first 5
            format_id = fmt.get("format_id")
            name = fmt.get("name", "Unknown")
            preview = fmt.get("preview_data", {})
            preview_url = preview.get("preview_url")

            if preview_url:
                print(f"📋 {name}")
                print(f"   Format ID: {format_id}")
                print(f"   Preview URL: {preview_url}")
                print(f"   Expires: {preview.get('expires_at', 'N/A')}")
                print()

                preview_data.append(
                    {
                        "format_id": format_id,
                        "name": name,
                        "preview_url": preview_url,
                        "width": 300,
                        "height": 400,
                    }
                )

        # Save to JSON for the web component demo
        output_file = Path(__file__).parent / "preview_urls.json"
        with open(output_file, "w") as f:
            json.dump(preview_data, f, indent=2)

        print(f"💾 Saved preview URLs to: {output_file}")
        print("\n🌐 Open examples/web_component_demo.html in a browser to see the previews!")

    except Exception as e:
        print(f"❌ Error: {e}")
        raise
    finally:
        await creative_agent.close()


if __name__ == "__main__":
    asyncio.run(main())

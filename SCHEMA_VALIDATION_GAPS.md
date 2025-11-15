# Schema Validation Gaps Report

## Summary

Multiple AdCP JSON schemas document "mutually exclusive" field constraints in descriptions but **do not enforce them** through JSON Schema validation. This allows invalid data to pass schema validation.

## Impact

- **Validation fails silently** - invalid data passes schema checks
- **Runtime errors downstream** - applications must handle ambiguous data
- **Inconsistent implementations** - different clients may interpret differently
- **Poor DX** - validation errors caught late in development

## Issues Found

### 1. Product Schema: `publisher_properties` Items

**File**: `/schemas/v1/core/product.json` (lines 126-162)

**Problem**: The `property_ids` and `property_tags` fields are documented as mutually exclusive but both can be provided (or neither).

**Current Schema** (lines 130-158):
```json
{
  "publisher_properties": {
    "items": {
      "properties": {
        "property_ids": {
          "description": "Specific property IDs from the publisher's adagents.json. Mutually exclusive with property_tags.",
          ...
        },
        "property_tags": {
          "description": "Property tags from the publisher's adagents.json. Product covers all properties with these tags. Mutually exclusive with property_ids.",
          ...
        },
        "publisher_domain": { ... }
      },
      "required": ["publisher_domain"],
      "type": "object"
    }
  }
}
```

**Required Fix**:
```json
{
  "publisher_properties": {
    "items": {
      "properties": { ... },
      "required": ["publisher_domain"],
      "oneOf": [
        {
          "required": ["property_ids"],
          "not": { "required": ["property_tags"] }
        },
        {
          "required": ["property_tags"],
          "not": { "required": ["property_ids"] }
        }
      ],
      "additionalProperties": false,
      "type": "object"
    }
  }
}
```

### 2. AdAgents Schema: Agent Authorization Fields

**File**: `/schemas/v1/adagents.json` (lines 200-279)

**Problem**: The agent authorization has **four** mutually exclusive fields (`properties`, `property_ids`, `property_tags`, `publisher_properties`) but no validation enforcing this.

**Current Schema** (lines 207-269):
```json
{
  "properties": {
    "properties": {
      "description": "Specific properties this agent is authorized for (alternative to property_ids/property_tags). Mutually exclusive with property_ids and property_tags fields.",
      ...
    },
    "property_ids": {
      "description": "Property IDs this agent is authorized for. Resolved against the top-level properties array in this file. Mutually exclusive with property_tags and properties fields.",
      ...
    },
    "property_tags": {
      "description": "Tags identifying which properties this agent is authorized for. Resolved against the top-level properties array in this file using tag matching. Mutually exclusive with property_ids and properties fields.",
      ...
    },
    "publisher_properties": {
      "description": "Properties from other publisher domains this agent is authorized for. Each entry specifies a publisher domain and which of their properties this agent can sell (by property_id or property_tags). Mutually exclusive with property_ids, property_tags, and properties fields.",
      ...
    }
  },
  "required": ["url", "authorized_for"]
}
```

**Required Fix**:
```json
{
  "properties": { ... },
  "required": ["url", "authorized_for"],
  "oneOf": [
    {
      "required": ["properties"],
      "not": {
        "anyOf": [
          { "required": ["property_ids"] },
          { "required": ["property_tags"] },
          { "required": ["publisher_properties"] }
        ]
      }
    },
    {
      "required": ["property_ids"],
      "not": {
        "anyOf": [
          { "required": ["properties"] },
          { "required": ["property_tags"] },
          { "required": ["publisher_properties"] }
        ]
      }
    },
    {
      "required": ["property_tags"],
      "not": {
        "anyOf": [
          { "required": ["properties"] },
          { "required": ["property_ids"] },
          { "required": ["publisher_properties"] }
        ]
      }
    },
    {
      "required": ["publisher_properties"],
      "not": {
        "anyOf": [
          { "required": ["properties"] },
          { "required": ["property_ids"] },
          { "required": ["property_tags"] }
        ]
      }
    }
  ]
}
```

### 3. AdAgents Schema: `publisher_properties` Items

**File**: `/schemas/v1/adagents.json` (lines 233-269)

**Problem**: Within `publisher_properties` array items, `property_ids` and `property_tags` are mutually exclusive but not validated (same as Product schema issue #1).

**Current Schema** (lines 237-265):
```json
{
  "publisher_properties": {
    "items": {
      "properties": {
        "property_ids": {
          "description": "Specific property IDs from the publisher's adagents.json properties array. Mutually exclusive with property_tags.",
          ...
        },
        "property_tags": {
          "description": "Property tags from the publisher's adagents.json tags. Agent is authorized for all properties with these tags. Mutually exclusive with property_ids.",
          ...
        },
        "publisher_domain": { ... }
      },
      "required": ["publisher_domain"],
      "additionalProperties": false,
      "type": "object"
    }
  }
}
```

**Required Fix**: Same as Product schema fix (add `oneOf` constraint).

## Recommended Actions

1. **Report to upstream** - File issue with adcontextprotocol/adcp repository
2. **Add runtime validation** - Python SDK should validate these constraints even if schemas don't
3. **Document workaround** - Add validation in SDK with clear error messages
4. **Track schema version** - When upstream fixes schemas, update SDK to rely on schema validation

## Example Invalid Data That Currently Passes Validation

### Product with both `property_ids` and `property_tags`:
```json
{
  "product_id": "test",
  "publisher_properties": [
    {
      "publisher_domain": "example.com",
      "property_ids": ["site1", "site2"],
      "property_tags": ["news", "sports"]  // Should be rejected!
    }
  ]
}
```

### Product with neither `property_ids` nor `property_tags`:
```json
{
  "product_id": "test",
  "publisher_properties": [
    {
      "publisher_domain": "example.com"  // Should require one!
    }
  ]
}
```

### Agent with multiple authorization methods:
```json
{
  "url": "https://agent.example.com",
  "authorized_for": "All properties",
  "property_ids": ["site1"],
  "property_tags": ["news"],  // Should be rejected!
  "properties": [{ ... }]     // Should be rejected!
}
```

## Testing

Run schema validation against these invalid examples to confirm they incorrectly pass.

## References

- Product schema: `https://adcontextprotocol.org/schemas/v1/core/product.json`
- AdAgents schema: `https://adcontextprotocol.org/schemas/v1/adagents.json`
- JSON Schema `oneOf`: https://json-schema.org/understanding-json-schema/reference/combining#oneOf

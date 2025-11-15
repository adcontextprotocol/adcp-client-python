# Upstream Schema Issues: Missing Mutual Exclusivity Validation

## Summary

Multiple AdCP JSON schemas document "mutually exclusive" field constraints in their `description` fields but **do not enforce them** through JSON Schema validation constructs (`oneOf`, `not`, etc.). This allows invalid data to pass schema validation, leading to ambiguous or contradictory data structures.

## Impact

- **Silent validation failures** - Invalid data passes schema checks and causes runtime errors downstream
- **Implementation inconsistencies** - Different clients may interpret ambiguous data differently
- **Poor developer experience** - Validation errors caught late in development instead of at API boundaries
- **Security concerns** - Ambiguous authorization data in adagents.json could lead to incorrect access control

## Issues Found

### Issue 1: Product Schema - publisher_properties Mutual Exclusivity

**File**: `/schemas/v1/core/product.json`
**Lines**: 126-162
**Schema URL**: https://adcontextprotocol.org/schemas/v1/core/product.json

**Problem**: Within `publisher_properties` array items, the fields `property_ids` and `property_tags` are documented as mutually exclusive but both can be provided (or neither).

**Current Schema**:
```json
{
  "publisher_properties": {
    "description": "Publisher properties covered by this product...",
    "items": {
      "additionalProperties": false,
      "properties": {
        "property_ids": {
          "description": "Specific property IDs from the publisher's adagents.json. Mutually exclusive with property_tags.",
          "items": {
            "pattern": "^[a-z0-9_]+$",
            "type": "string"
          },
          "minItems": 1,
          "type": "array"
        },
        "property_tags": {
          "description": "Property tags from the publisher's adagents.json. Product covers all properties with these tags. Mutually exclusive with property_ids.",
          "items": {
            "pattern": "^[a-z0-9_]+$",
            "type": "string"
          },
          "minItems": 1,
          "type": "array"
        },
        "publisher_domain": {
          "description": "Domain where publisher's adagents.json is hosted (e.g., 'cnn.com')",
          "pattern": "^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$",
          "type": "string"
        }
      },
      "required": [
        "publisher_domain"
      ],
      "type": "object"
    },
    "minItems": 1,
    "type": "array"
  }
}
```

**Required Fix**:
```json
{
  "publisher_properties": {
    "items": {
      "properties": { /* ... same as above ... */ },
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

**Invalid Data That Currently Passes**:

Example 1 - Both fields present:
```json
{
  "product_id": "test",
  "publisher_properties": [
    {
      "publisher_domain": "example.com",
      "property_ids": ["site1", "site2"],
      "property_tags": ["news", "sports"]
    }
  ]
}
```

Example 2 - Neither field present:
```json
{
  "product_id": "test",
  "publisher_properties": [
    {
      "publisher_domain": "example.com"
    }
  ]
}
```

---

### Issue 2: AdAgents Schema - Agent Authorization 4-Way Mutual Exclusivity

**File**: `/schemas/v1/adagents.json`
**Lines**: 200-279 (within `authorized_agents` array items)
**Schema URL**: https://adcontextprotocol.org/schemas/v1/adagents.json

**Problem**: Agent authorization has **four** mutually exclusive fields (`properties`, `property_ids`, `property_tags`, `publisher_properties`) but no validation enforcing that exactly one must be present.

**Current Schema**:
```json
{
  "authorized_agents": {
    "items": {
      "properties": {
        "url": { "type": "string", "format": "uri" },
        "authorized_for": { "type": "string" },
        "properties": {
          "description": "Specific properties this agent is authorized for (alternative to property_ids/property_tags). Mutually exclusive with property_ids and property_tags fields.",
          "items": { "$ref": "/schemas/v1/core/property.json" },
          "minItems": 1,
          "type": "array"
        },
        "property_ids": {
          "description": "Property IDs this agent is authorized for. Resolved against the top-level properties array in this file. Mutually exclusive with property_tags and properties fields.",
          "items": { "pattern": "^[a-z0-9_]+$", "type": "string" },
          "minItems": 1,
          "type": "array"
        },
        "property_tags": {
          "description": "Tags identifying which properties this agent is authorized for. Resolved against the top-level properties array in this file using tag matching. Mutually exclusive with property_ids and properties fields.",
          "items": { "pattern": "^[a-z0-9_]+$", "type": "string" },
          "minItems": 1,
          "type": "array"
        },
        "publisher_properties": {
          "description": "Properties from other publisher domains this agent is authorized for. Each entry specifies a publisher domain and which of their properties this agent can sell (by property_id or property_tags). Mutually exclusive with property_ids, property_tags, and properties fields.",
          "items": { /* nested object */ },
          "minItems": 1,
          "type": "array"
        }
      },
      "required": ["url", "authorized_for"]
    }
  }
}
```

**Required Fix**:
```json
{
  "authorized_agents": {
    "items": {
      "properties": { /* ... same as above ... */ },
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
  }
}
```

**Invalid Data That Currently Passes**:

Example 1 - Multiple authorization methods:
```json
{
  "authorized_agents": [
    {
      "url": "https://agent.example.com",
      "authorized_for": "All properties",
      "property_ids": ["site1"],
      "property_tags": ["news"],
      "properties": [{ "property_id": "site2", "type": "website" }]
    }
  ]
}
```

Example 2 - No authorization method:
```json
{
  "authorized_agents": [
    {
      "url": "https://agent.example.com",
      "authorized_for": "All properties"
    }
  ]
}
```

---

### Issue 3: AdAgents Schema - publisher_properties Items Mutual Exclusivity

**File**: `/schemas/v1/adagents.json`
**Lines**: 233-269 (within `publisher_properties` array)
**Schema URL**: https://adcontextprotocol.org/schemas/v1/adagents.json

**Problem**: This is the same issue as Issue #1 but within the adagents.json `publisher_properties` structure. The `property_ids` and `property_tags` fields are mutually exclusive but not validated.

**Current Schema**:
```json
{
  "publisher_properties": {
    "items": {
      "additionalProperties": false,
      "properties": {
        "property_ids": {
          "description": "Specific property IDs from the publisher's adagents.json properties array. Mutually exclusive with property_tags.",
          "items": { "pattern": "^[a-z0-9_]+$", "type": "string" },
          "minItems": 1,
          "type": "array"
        },
        "property_tags": {
          "description": "Property tags from the publisher's adagents.json tags. Agent is authorized for all properties with these tags. Mutually exclusive with property_ids.",
          "items": { "pattern": "^[a-z0-9_]+$", "type": "string" },
          "minItems": 1,
          "type": "array"
        },
        "publisher_domain": {
          "description": "Domain where the publisher's adagents.json is hosted (e.g., 'cnn.com')",
          "pattern": "^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$",
          "type": "string"
        }
      },
      "required": ["publisher_domain"],
      "type": "object"
    }
  }
}
```

**Required Fix**: Same as Issue #1 (add `oneOf` constraint)

---

## Comparison: Correct Implementation Example

The `activation-key.json` schema **correctly** implements mutual exclusivity using `oneOf`:

**File**: `/schemas/v1/core/activation-key.json`
**Lines**: 5-47

```json
{
  "description": "Universal identifier for using a signal on a destination platform. Can be either a segment ID or a key-value pair depending on the platform's targeting mechanism.",
  "oneOf": [
    {
      "additionalProperties": false,
      "properties": {
        "segment_id": { "description": "The platform-specific segment identifier...", "type": "string" },
        "type": { "const": "segment_id", "type": "string" }
      },
      "required": ["type", "segment_id"]
    },
    {
      "additionalProperties": false,
      "properties": {
        "key": { "description": "The targeting parameter key", "type": "string" },
        "type": { "const": "key_value", "type": "string" },
        "value": { "description": "The targeting parameter value", "type": "string" }
      },
      "required": ["type", "key", "value"]
    }
  ],
  "type": "object"
}
```

This schema correctly enforces that exactly one of the two variants must be used.

---

## Testing Validation

To verify these issues exist, run schema validation against the invalid examples above. They will incorrectly pass validation.

**Python Example**:
```python
import jsonschema
import requests

# Fetch schema
schema_url = "https://adcontextprotocol.org/schemas/v1/core/product.json"
schema = requests.get(schema_url).json()

# Invalid data (both property_ids AND property_tags)
invalid_product = {
    "product_id": "test",
    "name": "Test Product",
    "description": "Test",
    "publisher_properties": [{
        "publisher_domain": "example.com",
        "property_ids": ["site1"],
        "property_tags": ["news"]  # Should be rejected!
    }],
    "format_ids": [],
    "delivery_type": "guaranteed",
    "delivery_measurement": {"provider": "Test"},
    "pricing_options": []
}

# This will NOT raise an error (but it should!)
jsonschema.validate(invalid_product, schema)
print("❌ Validation passed (should have failed)")
```

---

## Recommended Actions

1. **Update schemas** to add `oneOf` constraints for all documented mutual exclusivity relationships
2. **Add test cases** to schema test suite validating that mutually exclusive fields are properly rejected
3. **Document in changelog** as a breaking change (invalid data that previously passed will now be rejected)
4. **Consider schema version bump** if this is considered a breaking change to the validation behavior

---

## References

- JSON Schema `oneOf` documentation: https://json-schema.org/understanding-json-schema/reference/combining#oneOf
- AdCP Schema Registry: https://adcontextprotocol.org/schemas/v1/index.json
- Related schemas: product.json, adagents.json, activation-key.json (correct example)

---

## Client-Side Workaround

Until these schema fixes are deployed, the Python SDK (adcp-client-python v2.0.0+) implements runtime validation to catch these issues:

```python
from adcp.validation import validate_product, validate_adagents

# Validate product data
try:
    validate_product(product_data)
except ValidationError as e:
    print(f"Invalid product: {e}")

# Validate adagents data (automatically applied by fetch_adagents())
try:
    validate_adagents(adagents_data)
except ValidationError as e:
    print(f"Invalid adagents.json: {e}")
```

This provides immediate protection while waiting for upstream schema fixes.

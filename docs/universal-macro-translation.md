# Universal macro translation

Sellers can translate AdCP universal macros in pixel URL query values before
publishing a creative. The helper preserves the path, query keys, fragments,
and literal parameters byte-for-byte while translating only mapped universal
macros in parameter values.

```python
from adcp.substitution import (
    NativeMacroMapping,
    ValueMacroMapping,
    translate_universal_macros,
)

result = translate_universal_macros(
    "https://pixel.example/i?buy={MEDIA_BUY_ID}&gdpr={GDPR_CONSENT}",
    {
        "{MEDIA_BUY_ID}": ValueMacroMapping(value="mb/123"),
        "{GDPR_CONSENT}": NativeMacroMapping(native="%%GDPR_CONSENT%%"),
    },
)

assert result.url == (
    "https://pixel.example/i?buy=mb%2F123&gdpr=%%GDPR_CONSENT%%"
)
```

`ValueMacroMapping` is for literal data. It UTF-8 encodes the value and
percent-escapes every byte outside the RFC 3986 unreserved set. Use
`NativeMacroMapping` only for a downstream ad-server token that must be
inserted verbatim.

## Trust boundary and diagnostics

Native mappings bypass URL encoding. Before producing any URL, the helper
therefore rejects U+0000–U+001F and U+007F in every native mapping, including
unused entries. Catch `UniversalMacroTranslationError` and inspect its stable
`code` (`unsafe_native_mapping`) and `macro` attributes if this is an expected
configuration boundary.

Always inspect the result diagnostics before publishing the tracker:

- `dropped_params` lists each query-parameter occurrence removed because it
  contained an unmapped macro.
- `unmapped_macros` lists the missing macros once, in first query occurrence
  order.
- `dropped_consent_macros` highlights missing consent mappings.
- `frozen_consent_macros` highlights consent macros supplied as literal
  `value` mappings. They are encoded and emitted, but may freeze consent that
  should instead be resolved per impression.
- `suspect_native_values` highlights literal values shaped like common native
  ad-server tokens and is ordered by mapping insertion order.

The translator is single-pass: macro-shaped text inside a mapped value is data,
not another substitution. A bare trailing `?` is normalized away. The behavior
is continuously checked against the shared AdCP 3.2 compliance fixture from
[AdCP #6674](https://github.com/adcontextprotocol/adcp/issues/6674).

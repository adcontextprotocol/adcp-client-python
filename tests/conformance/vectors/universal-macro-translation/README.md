# Universal macro translation vectors

These files are vendored verbatim from the AdCP compliance source at commit
[`acc022a53fad8ecab877d374df1760fef756325f`](https://github.com/adcontextprotocol/adcp/commit/acc022a53fad8ecab877d374df1760fef756325f):

- `static/compliance/source/test-vectors/universal-macro-translation.json`
- `static/compliance/source/test-vectors/universal-macro-translation.schema.json`

The test suite pins their SHA-256 digests and executes the fixture directly
against `adcp.substitution.translate_universal_macros`. Update the source
commit, both files, and both digests together when deliberately adopting a new
fixture revision. Do not edit or add expected results in Python.

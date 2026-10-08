# Retained 3.1.15 source contract

This snapshot preserves the SDK's previously shipped `schemas/cache/3.1`
tree byte for byte. The signed products-only compatibility vectors and
persisted purchase continuations require this exact source release.

The `3.1` cache tracks the signed 3.1.27 maintenance release. An explicit
`3.1.15` selector resolves here so replay remains bound to its original
schema contract. Keep this snapshot when synchronizing newer maintenance
releases; do not rewrite it to make older continuations validate.

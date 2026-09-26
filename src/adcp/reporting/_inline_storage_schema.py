"""Required catalog objects from reporting_inline_storage.sql on PostgreSQL 16.

Generated from a clean migration; unrelated adopter objects are permitted.
"""

REQUIRED_OBJECTS: dict[str, dict[str, str | bool]] = {
    "column:reporting_inline_objects.account_id": {
        "enabled": True,
        "fingerprint": "fdc4b549138bf90a5af5ed00b87a86ff9a0cbea553e7f017b4d95510708f6f88",
    },
    "column:reporting_inline_objects.payload": {
        "enabled": True,
        "fingerprint": "554c34e416bd4546469b42d5773bc7c59b2104366ffa5259a2838c64cd826e57",
    },
    "column:reporting_inline_objects.payload_sha256": {
        "enabled": True,
        "fingerprint": "fdc4b549138bf90a5af5ed00b87a86ff9a0cbea553e7f017b4d95510708f6f88",
    },
    "column:reporting_inline_seals.account_id": {
        "enabled": True,
        "fingerprint": "fdc4b549138bf90a5af5ed00b87a86ff9a0cbea553e7f017b4d95510708f6f88",
    },
    "column:reporting_inline_seals.byte_count": {
        "enabled": True,
        "fingerprint": "64be57437fdc0a07a97985c2aa058031f8082db7251bdb4d5afa1a9b088de97a",
    },
    "column:reporting_inline_seals.manifest": {
        "enabled": True,
        "fingerprint": "554c34e416bd4546469b42d5773bc7c59b2104366ffa5259a2838c64cd826e57",
    },
    "column:reporting_inline_seals.manifest_sha256": {
        "enabled": True,
        "fingerprint": "fdc4b549138bf90a5af5ed00b87a86ff9a0cbea553e7f017b4d95510708f6f88",
    },
    "column:reporting_inline_seals.source_execution_key": {
        "enabled": True,
        "fingerprint": "fdc4b549138bf90a5af5ed00b87a86ff9a0cbea553e7f017b4d95510708f6f88",
    },
    "column:reporting_inline_seals.staged_commit_ref": {
        "enabled": True,
        "fingerprint": "fdc4b549138bf90a5af5ed00b87a86ff9a0cbea553e7f017b4d95510708f6f88",
    },
    "constraint:reporting_inline_objects.reporting_inline_objects_account": {
        "enabled": True,
        "fingerprint": "ff3e5aec84611ad080f664147be02d92ab106b3e2c9407bc63b5396bc0c8006f",
    },
    "constraint:reporting_inline_objects.reporting_inline_objects_bytes": {
        "enabled": True,
        "fingerprint": "5cf3d75f67d8c5c6631ba87b9d6d301a4f807f9ce619bc77e3b81f43707d6669",
    },
    "constraint:reporting_inline_objects.reporting_inline_objects_digest": {
        "enabled": True,
        "fingerprint": "3a303f02341c258a572f9eaf1e6869cdfd7547bc2932c44c41a8d1a7eed280a2",
    },
    "constraint:reporting_inline_objects.reporting_inline_objects_pk": {
        "enabled": True,
        "fingerprint": "52c73c955e80fe2c8de482afc62851429635aa24f3a10cd4e15925eaa5a05bb3",
    },
    "constraint:reporting_inline_objects.reporting_inline_objects_size": {
        "enabled": True,
        "fingerprint": "7c4fb6f6c0ff3c05be6ae3986c8a849e886e06e7088f5d09fef81b9f3b326ed8",
    },
    "constraint:reporting_inline_seals.reporting_inline_seals_account": {
        "enabled": True,
        "fingerprint": "ff3e5aec84611ad080f664147be02d92ab106b3e2c9407bc63b5396bc0c8006f",
    },
    "constraint:reporting_inline_seals.reporting_inline_seals_account_binding": {
        "enabled": True,
        "fingerprint": "ae8a06d397d61baba67cc4e77001596682a09ebecac0babaa0268c79ac7f242d",
    },
    "constraint:reporting_inline_seals.reporting_inline_seals_bytes": {
        "enabled": True,
        "fingerprint": "2cf54d2016b1d93e6cc2e8f6538ed87fc5b04b980048275210661926eeb49296",
    },
    "constraint:reporting_inline_seals.reporting_inline_seals_count": {
        "enabled": True,
        "fingerprint": "1e87e7574019dc01b1b39584ef6a420e82330e7089490b928f04f5bce4482f7c",
    },
    "constraint:reporting_inline_seals.reporting_inline_seals_digest": {
        "enabled": True,
        "fingerprint": "6d150c5fa94598b18c9adf65800d6ddd58479f97cb9b845b3d0f7a791aafced4",
    },
    "constraint:reporting_inline_seals.reporting_inline_seals_key": {
        "enabled": True,
        "fingerprint": "fadbcdf37ecabe857d9111775f689255637a0cef3028301593c285b69a0551a2",
    },
    "constraint:reporting_inline_seals.reporting_inline_seals_key_binding": {
        "enabled": True,
        "fingerprint": "897155db2cfdde3b4ed88ea3066c43b8938b9961e9bcca08eac4acbe71e5d50b",
    },
    "constraint:reporting_inline_seals.reporting_inline_seals_pk": {
        "enabled": True,
        "fingerprint": "123e2c394bd92bfab2862432ed6191484dd5438ca9b9f6372108b59c901b4248",
    },
    "constraint:reporting_inline_seals.reporting_inline_seals_ref": {
        "enabled": True,
        "fingerprint": "6ef63ffe9db91b8fa1fe7f75817e958e343cfbc0f8d9134a188fc65ce7d39a88",
    },
    "constraint:reporting_inline_seals.reporting_inline_seals_size": {
        "enabled": True,
        "fingerprint": "b6c20825c79f2f4cbee21b439842ace6958549b83913f82d958de73980754ab2",
    },
    "function:reporting_inline_immutable()": {
        "enabled": True,
        "fingerprint": "f2baed2e6158fc173155a71c5dedd3423b32d5923582600ae3c7062086c8ba41",
    },
    "index:reporting_inline_objects.reporting_inline_objects_pk": {
        "enabled": True,
        "fingerprint": "ba41f90f12a63ad244a676e7d7ae37aacca579d32f6a1ced16a5676c420083d0",
    },
    "index:reporting_inline_seals.reporting_inline_seals_pk": {
        "enabled": True,
        "fingerprint": "0816d86030df87c7d77ed0df4cec81b2a30e61d959fb53063750e4c2934623aa",
    },
    "table:reporting_inline_objects": {
        "enabled": True,
        "fingerprint": "1f824779ff80f110344420b019786663d8c9beaad230da90e0439795e734ccda",
    },
    "table:reporting_inline_seals": {
        "enabled": True,
        "fingerprint": "1f824779ff80f110344420b019786663d8c9beaad230da90e0439795e734ccda",
    },
    "trigger:reporting_inline_objects.reporting_inline_objects_immutable": {
        "enabled": True,
        "fingerprint": "da630163871019e8457c2d54b41f23e7f182ad231e539d4f57360ea45c39a1c1",
    },
    "trigger:reporting_inline_seals.reporting_inline_seals_immutable": {
        "enabled": True,
        "fingerprint": "201a920cf5c5343295735a14da5eee8def6fd80bd72826628ba3706ebc75d9d2",
    },
}

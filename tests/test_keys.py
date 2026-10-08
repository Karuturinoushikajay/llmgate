from llmgate.storage.keys import generate_api_key, hash_api_key, key_prefix


def test_generated_key_is_prefixed_and_hashed() -> None:
    raw = generate_api_key()
    assert raw.startswith("sk-lg-")
    digest = hash_api_key(raw, "pepper")
    assert digest != raw
    assert digest == hash_api_key(raw, "pepper")
    assert digest != hash_api_key(raw, "other-pepper")
    assert key_prefix(raw) == raw[:12]
    assert len(digest) == 64

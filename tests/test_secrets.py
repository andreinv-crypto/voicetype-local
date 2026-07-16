from __future__ import annotations

import pytest

from voicetype_local.secrets import DpapiSecretStore


class _Protector:
    def protect(self, value: bytes) -> bytes:
        return b"protected:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        assert value.startswith(b"protected:")
        return value.removeprefix(b"protected:")[::-1]


def test_secret_store_never_writes_plaintext(tmp_path) -> None:
    store = DpapiSecretStore(tmp_path, _Protector())
    store.set("openai_api_key", "sk-not-a-real-key")

    raw = (tmp_path / "openai_api_key.dpapi").read_bytes()
    assert b"sk-not-a-real-key" not in raw
    assert store.get("openai_api_key") == "sk-not-a-real-key"

    store.delete("openai_api_key")
    assert store.get("openai_api_key") is None


@pytest.mark.parametrize("name", ["../key", "A KEY", "", "x" * 65])
def test_secret_names_cannot_escape_directory(tmp_path, name: str) -> None:
    store = DpapiSecretStore(tmp_path, _Protector())
    with pytest.raises(ValueError):
        store.set(name, "value")

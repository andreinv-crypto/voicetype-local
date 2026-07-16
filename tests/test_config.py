import json

from voicetype_local.config import SettingsStore


def test_invalid_values_fall_back(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "language": "xx",
                "model_name": "huge",
                "compute_type": "unsafe",
                "cpu_threads": 999,
                "unknown": "ignored",
            }
        ),
        encoding="utf-8",
    )

    settings = SettingsStore(path).get()

    assert settings.language == "auto"
    assert settings.model_name == "small"
    assert settings.compute_type == "int8"
    assert settings.cpu_threads == 16


def test_update_is_saved_atomically(tmp_path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)

    store.update(language="es")

    assert SettingsStore(path).get().language == "es"
    assert not path.with_suffix(".tmp").exists()


from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from faster_whisper.utils import download_model


MODEL_REVISIONS = {
    "small": "536b0662742c02347bc0e980a01041f333bce120",
}

MODEL_FILES_SHA256 = {
    "small": {
        "config.json": "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828",
        "model.bin": "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
        "tokenizer.json": "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab",
        "vocabulary.txt": "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913",
    },
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_model(model_name: str, model_dir: Path) -> None:
    expected_files = MODEL_FILES_SHA256.get(model_name)
    if not expected_files:
        raise SystemExit(f"No pinned checksums for model: {model_name}")
    for filename, expected in expected_files.items():
        path = model_dir / filename
        if not path.exists() or sha256_file(path) != expected:
            raise SystemExit(f"Model checksum mismatch: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download an offline faster-whisper model.")
    parser.add_argument("--model", choices=("small",), default="small")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = args.output.resolve()
    model_file = output / "model.bin"
    if model_file.exists():
        verify_model(args.model, output)
        print(f"Model already present: {output}")
        return

    output.mkdir(parents=True, exist_ok=True)
    print(f"Downloading multilingual model '{args.model}' to {output} ...")
    result = Path(
        download_model(
            args.model,
            output_dir=str(output),
            revision=MODEL_REVISIONS.get(args.model),
        )
    )
    if not (result / "model.bin").exists():
        raise SystemExit(f"Download finished but model.bin is missing in {result}")
    verify_model(args.model, result)
    print(f"Model ready: {result}")


if __name__ == "__main__":
    main()

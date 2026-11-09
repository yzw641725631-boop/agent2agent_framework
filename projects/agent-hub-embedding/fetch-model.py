"""Operator build step only; inference never downloads models or runs remote code."""

import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

manifest = json.loads(
    Path(__file__).with_name("model-manifest.json").read_text(encoding="utf-8")
)
directory = Path(sys.argv[1])
directory.mkdir(parents=True, exist_ok=True)
for name, digest in manifest["files"].items():
    target = directory / name
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
        continue
    path = "onnx/" + name if name.endswith(".onnx") else name
    url = f"https://huggingface.co/{manifest['repository']}/resolve/{manifest['revision']}/{path}"
    temporary = target.with_suffix(target.suffix + ".partial")
    with (
        urllib.request.urlopen(url, timeout=120) as response,
        temporary.open("wb") as stream,
    ):
        count = 0
        while chunk := response.read(1024 * 1024):
            count += len(chunk)
            if count > 100000000:
                raise ValueError("Artifact too large")
            stream.write(chunk)
    if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
        raise ValueError("Artifact hash mismatch")
    os.replace(temporary, target)
(directory / "manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
)
print("Pinned model verified.")

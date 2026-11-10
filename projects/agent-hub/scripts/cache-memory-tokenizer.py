"""Build-time, hash-pinned reference tokenizer download. Runtime never downloads it."""
import hashlib
import sys
import urllib.request
from pathlib import Path

TARGET_HASH = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
target = Path(sys.argv[1])
with urllib.request.urlopen(
    "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken", timeout=30
) as response:
    data = response.read(2000000)
if hashlib.sha256(data).hexdigest() != TARGET_HASH:
    raise SystemExit("Reference tokenizer hash mismatch")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_bytes(data)
print("Verified cl100k_base reference tokenizer.")

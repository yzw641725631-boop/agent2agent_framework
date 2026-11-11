"""Publish a complete certificate/key pair atomically; no Docker socket is used."""

import os
import shutil
import time
from pathlib import Path

source = Path("/etc/letsencrypt/live/agent-hub")
target = Path("/tls")
generation = target / ("issued-" + str(time.time_ns()))
generation.mkdir(mode=0o700)
for name in ("fullchain.pem", "privkey.pem"):
    shutil.copyfile(source / name, generation / name)
    (generation / name).chmod(0o600)
link = target / "current.next"
link.symlink_to(generation.name)
os.replace(link, target / "current")
print("TLS certificate pair published; gateway reloads after detecting the new certificate.")

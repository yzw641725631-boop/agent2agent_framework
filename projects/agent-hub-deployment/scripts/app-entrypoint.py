"""Read only the secrets granted to this process, then start the normal application."""

import os
import sys
from pathlib import Path
from urllib.parse import quote

role = os.environ["HUB_DB_ROLE"]
if role not in {"hub_owner", "hub_runtime", "hub_document_worker", "hub_memory_worker",
                "hub_embedding_worker", "hub_generation_worker"}:
    raise SystemExit("Unknown database role")
secrets = Path("/run/secrets")
password = (secrets / "database_password").read_text().strip()
os.environ["HUB_DATABASE_URL"] = "postgresql+psycopg://" + role + ":" + quote(password, safe="") + "@db:5432/agent_hub"
os.environ["HUB_SECRET"] = (secrets / "application_key").read_text().strip()
if (secrets / "bootstrap_token").is_file():
    os.environ["HUB_BOOTSTRAP_TOKEN"] = (secrets / "bootstrap_token").read_text().strip()
if len(sys.argv) < 2:
    raise SystemExit("Application command required")
os.execvp(sys.argv[1], sys.argv[1:])

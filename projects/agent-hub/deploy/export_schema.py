"""Export first-install SQL without opening a database connection.

Run with the project Python interpreter, then transfer the resulting SQL to the server.
The placeholder configuration is only for Alembic's offline compiler.
"""

import io
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

project = Path(__file__).resolve().parents[1]
os.environ.update(
    HUB_DATABASE_URL="postgresql+psycopg://unused/unused",
    HUB_SECRET="OFFLINE_SQL_EXPORT_NO_CREDENTIAL_0000000000",
    HUB_ENVIRONMENT="development",
    HUB_MAIL_MODE="smtp",
    HUB_PUBLIC_URL="http://127.0.0.1:8765",
)
output = io.StringIO()
command.upgrade(Config(str(project / "alembic.ini"), output_buffer=output), "head", sql=True)
target = Path(sys.argv[1]).resolve()
with target.open("x", encoding="utf-8", newline="\n") as file:
    file.write("-- Fresh server volume only. Object owner is the separate migration role.\n")
    file.write("CREATE EXTENSION IF NOT EXISTS vector;\n")
    file.write("SET ROLE hub_owner;\n")
    file.write(output.getvalue())
    file.write("\nRESET ROLE;\n")
print("Offline schema exported; no database connection was opened.")

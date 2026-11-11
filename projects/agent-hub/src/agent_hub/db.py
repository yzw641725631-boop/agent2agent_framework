import hashlib

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings


def session_factory(settings: Settings, *, security=False):
    engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        hide_parameters=True,
        pool_size=3 if security else 5,
        max_overflow=0 if security else 10,
        pool_timeout=3,
        connect_args={
            "connect_timeout": 5,
            # RLS-heavy interactive reads spend seconds compiling JIT code for
            # millisecond scans. Keep these bounded service sessions interpreted.
            "options": "-c timezone=UTC -c jit=off -c lock_timeout=3000 -c statement_timeout=10000 "
            "-c idle_in_transaction_session_timeout=15000",
        },
    )
    return sessionmaker(engine, expire_on_commit=False)


def coordinate(db: Session, *, user_ids=(), project_ids=(), resources=(), exclusive=True):
    """A shared recovery barrier, then sorted identity/resource locks, then project locks.

    Calling without subjects is reserved for bootstrap/local root recovery. Ordinary
    requests must pass subjects and never upgrade a shared lock inside a transaction.
    """
    subjects = {f"user:{value}" for value in user_ids if value}
    subjects.update(f"resource:{value}" for value in resources if value)
    projects = {f"project:{value}" for value in project_ids if value}
    if not subjects and not projects:
        db.execute(text("SELECT pg_advisory_xact_lock(7206110501)"))
        return
    db.execute(text("SELECT pg_advisory_xact_lock_shared(7206110501)"))
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    for scope in [*sorted(subjects), *sorted(projects)]:
        key = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True)
        db.execute(text(f"SELECT {function}(:key)"), {"key": key})


def context(db: Session, user_id, device_id, grant_id=None):
    for key, value in {"user_id": user_id, "device_id": device_id, "grant_id": grant_id}.items():
        db.execute(
            text("SELECT set_config(:key, :value, true)"),
            {"key": f"hub.{key}", "value": str(value) if value else ""},
        )

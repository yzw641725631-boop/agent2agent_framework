import json
import secrets
from pathlib import Path

import typer
import uvicorn
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select, text

from .config import get_settings
from .db import coordinate, session_factory
from .mail import Mailer
from .models import Device, User
from .schemas import NewUser
from .security import HubError, now, public_key, verify_password
from .service import Hub
from .site import SiteSettings

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8765):
    """Run the internal foundation API. Use an HTTPS gateway before remote access."""
    uvicorn.run("agent_hub.api:create_app", factory=True, host=host, port=port, access_log=False)


@app.command()
def migrate(config: Path = Path("alembic.ini")):
    """Apply migrations using the separate database owner URL."""
    command.upgrade(Config(str(config.resolve())), "head")


@app.command("document-worker")
def document_worker():
    """Process document jobs using the separate hub_indexer database role."""
    from .document_worker import DocumentWorker

    DocumentWorker(get_settings()).serve()


@app.command("memory-worker")
def memory_worker():
    """Extract incremental source-bound memory using the separate hub_memory role."""
    from .memory_worker import MemoryWorker

    MemoryWorker(get_settings()).serve()


@app.command("embedding-worker")
def embedding_worker():
    """Index source-bound cards and reviewed facts with the private CPU model."""
    from .embedding_worker import EmbeddingWorker
    EmbeddingWorker(get_settings()).serve()


@app.command("check-desktop-feed")
def check_desktop_feed():
    """Verify the public release feed and fail when fewer than 72 hours remain."""
    from .release_monitor import check

    try:
        result = check(get_settings().public_url)
    except Exception:
        typer.echo("Desktop release feed verification failed", err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(result))
    if result["remaining_hours"] < 72:
        typer.echo("Desktop release metadata needs renewal within 72 hours", err=True)
        raise typer.Exit(2)


@app.command("archive-audit")
def archive_audit(quarantine_orphans: bool = False):
    """Verify every encrypted code artifact as database owner; optionally quarantine old orphans."""
    from .archive_maintenance import audit

    try:
        settings = get_settings()
        result = audit(settings, session_factory(settings), quarantine_orphans=quarantine_orphans)
    except Exception:
        typer.echo("Artifact verification could not complete; retained existing files", err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(result))
    if not result["healthy"]:
        raise typer.Exit(2)


@app.command("device-key")
def device_key(output: Path):
    """Create an encrypted P-256 key and its public JWK; never overwrite existing files."""
    password = typer.prompt(
        "Key passphrase (at least 12 characters)", hide_input=True, confirmation_prompt=True
    )
    if len(password) < 12:
        raise typer.BadParameter("Passphrase is too short")
    output.mkdir(parents=True, exist_ok=True)
    private_path, public_path = output / "device.pem", output / "public.jwk.json"
    if private_path.exists() or public_path.exists():
        raise typer.BadParameter("Key files already exist")
    key = ec.generate_private_key(ec.SECP256R1())
    import jwt

    public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    with private_path.open("xb") as file:
        private_path.chmod(0o600)
        file.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.BestAvailableEncryption(password.encode()),
            )
        )
    with public_path.open("x", encoding="utf-8") as file:
        json.dump(public, file, indent=2)
    typer.echo(f"Encrypted key: {private_path.resolve()}\nPublic JWK: {public_path.resolve()}")


def check_owner(db):
    if not db.scalar(
        text("SELECT pg_get_userbyid(relowner)=current_user FROM pg_class WHERE oid='public.users'::regclass")
    ):
        raise HubError("LOCAL_DATABASE_OWNER_REQUIRED")


def email_proof(cfg, email):
    code = f"{secrets.randbelow(100000000):08d}"
    with session_factory(cfg)() as db:
        smtp_config = SiteSettings(cfg).read(db)[0]
    Mailer(cfg).send_code(email, code, smtp_config)
    for _ in range(3):
        if secrets.compare_digest(typer.prompt("Email code", hide_input=True), code):
            return
    raise HubError("EMAIL_VERIFICATION_FAILED")


@app.command()
def bootstrap(login: str, email: str, public_jwk: Path, os: str = typer.Option(..., help="windows or macos")):
    """Locally initialize the sole super administrator after real email verification."""
    if os not in {"windows", "macos"}:
        raise typer.BadParameter("OS must be windows or macos")
    cfg = get_settings()
    factory = session_factory(cfg)
    with factory() as db:
        check_owner(db)
        if db.scalar(select(User.id).where(User.platform_role == "superadmin")):
            raise HubError("SUPERADMIN_ALREADY_EXISTS", 409)
    password = typer.prompt("Super administrator password", hide_input=True, confirmation_prompt=True)
    validated = NewUser(login=login, email=email, password=password)
    jwk = json.loads(public_jwk.read_text(encoding="utf-8"))
    _, thumb = public_key(jwk)
    email_proof(cfg, str(validated.email))
    with factory.begin() as db:
        coordinate(db)
        user, device = Hub(cfg, Mailer(cfg)).bootstrap(
            db, validated.login, password, str(validated.email), jwk, thumb, os
        )
        typer.echo(f"Super administrator {user.id}; approved device {device.id}")


@app.command("recover-root")
def recover_root(login: str, public_jwk: Path, os: str = typer.Option(...)):
    """Local database-owner recovery: password + email OTP; invalidates all older root sessions."""
    if os not in {"windows", "macos"}:
        raise typer.BadParameter("OS must be windows or macos")
    cfg = get_settings()
    factory = session_factory(cfg)
    password = typer.prompt("Super administrator password", hide_input=True)
    with factory() as db:
        check_owner(db)
        user = db.scalar(select(User).where(User.login == login, User.platform_role == "superadmin"))
        if not user or not verify_password(user.password_hash, password):
            raise HubError("INVALID_CREDENTIALS", 401)
        user_id, email = user.id, user.email
    jwk = json.loads(public_jwk.read_text(encoding="utf-8"))
    _, thumb = public_key(jwk)
    email_proof(cfg, email)
    with factory.begin() as db:
        coordinate(db)
        user = db.get(User, user_id)
        user.auth_epoch += 1
        for old in db.scalars(select(Device).where(Device.user_id == user.id)):
            old.status, old.epoch = "revoked", old.epoch + 1
        if db.scalar(select(Device.id).where(Device.thumbprint == thumb)):
            raise HubError("RECOVERY_REQUIRES_A_NEW_KEY", 409)
        d = Device(
            user_id=user.id,
            public_jwk=jwk,
            thumbprint=thumb,
            name="recovered-device",
            os=os,
            status="approved",
            approved_by=user.id,
            email_verified_at=now(),
        )
        db.add(d)
        db.flush()
        hub = Hub(cfg, Mailer(cfg))
        hub.revoke_grants(db, user_id=user.id)
        hub.audit(db, user.id, "superadmin.recovered_locally", d.id, "local-recovery")
        typer.echo(f"Recovered device {d.id}. Older sessions are invalid.")


if __name__ == "__main__":
    app()

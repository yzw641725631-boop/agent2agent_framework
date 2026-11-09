#!/usr/bin/env python3
# ruff: noqa: UP017
"""Install on a different host. Poll TLS health and notify a configured HTTPS webhook.

No business content, passwords, token or response body enters notification or stdout.
The webhook URL and optional bearer token are protected operator configuration.
"""

import datetime
import hashlib
import json
import os
import ssl
import stat
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Monitor redirects are not accepted")


def request(url, *, payload=None, token=None):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("TLS endpoint required")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(
        Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers),
        timeout=10,
    ) as response:
        body = response.read(16385)
        if len(body) > 16384:
            raise ValueError("Oversized health response")
        return json.loads(body) if payload is None else None


def check(config, previous, transport=request, time=None):
    time = time or datetime.datetime.now(datetime.timezone.utc)
    healthy = False
    try:
        healthy = transport(config["health_url"]).get("status") == "ok"
    except Exception:
        pass
    status = "ok" if healthy else "unavailable"
    prior = previous.get("notified_status", previous.get("status"))
    last_sent = previous.get("last_sent_at")
    due = not last_sent or (time - datetime.datetime.fromisoformat(last_sent)).total_seconds() >= 3600
    send = status != prior or not healthy and due or previous.get("delivery_confirmed") is False
    state = {
        "schema": 1,
        "status": status,
        "notified_status": prior,
        "checked_at": time.isoformat(),
        "last_sent_at": last_sent,
        "delivery_confirmed": previous.get("delivery_confirmed", False),
    }
    if send:
        pending = previous.get("pending_event")
        event = (
            pending
            if isinstance(pending, dict) and pending.get("status") == status
            else {
                "service": "Agent Hub",
                "status": status,
                "time": time.isoformat(),
                "event_id": hashlib.sha256((status + ":" + time.isoformat()).encode()).hexdigest(),
            }
        )
        try:
            transport(config["webhook_url"], payload=event, token=config.get("webhook_token"))
            state.update(last_sent_at=time.isoformat(), notified_status=status, delivery_confirmed=True)
        except Exception:
            state.update(delivery_confirmed=False, pending_event=event)
    return state


def main():
    import fcntl

    base = Path("/opt/agent-hub-external-watch")
    file = base / "config.json"
    if any(item.is_symlink() for item in [file, *file.parents]):
        raise ValueError("Real operator paths required")
    info = file.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o077
        or info.st_uid != os.geteuid()
        or info.st_size > 16384
    ):
        raise ValueError("Protected configuration required")
    config = json.loads(file.read_text())
    if set(config) - {"health_url", "webhook_url", "webhook_token"} or not {
        "health_url",
        "webhook_url",
    } <= set(config):
        raise ValueError("Invalid monitor configuration")
    if any(
        not isinstance(value, str) or not value or len(value) > 4096 or "\n" in value or "\r" in value
        for value in config.values()
    ):
        raise ValueError("Invalid monitor configuration")
    if urlsplit(config["health_url"]).hostname == urlsplit(config["webhook_url"]).hostname:
        raise ValueError("Alert receiver must be independent of the monitored server")
    state_file = base / "state.json"
    if state_file.is_symlink() or (base / "monitor.lock").is_symlink():
        raise ValueError("Real monitor paths required")
    with (base / "monitor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if state_file.exists() and (not state_file.is_file() or state_file.stat().st_size > 16384):
            raise ValueError("Invalid monitor state")
        previous = json.loads(state_file.read_text()) if state_file.exists() else {}
        result = check(config, previous)
        descriptor, temporary = tempfile.mkstemp(prefix="state-", dir=str(base))
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(result, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, str(state_file))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print(json.dumps({"status": result["status"], "delivery_confirmed": result["delivery_confirmed"]}))
    return 0 if result["status"] == "ok" and result["delivery_confirmed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("External health or notification check did not complete.")
        raise SystemExit(1) from None

"""Load the YAML broker catalog into the DB without clobbering local edits."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import yaml

CATALOG_NAME = "brokers.yaml"


def bundled_path() -> Path:
    """Locate the shipped catalog.

    It lives inside the package rather than beside it, so that it survives a
    real `pip install` - deriving it from __file__'s grandparent only ever
    worked from a source checkout.
    """
    try:
        from importlib.resources import files
    except ImportError:  # pragma: no cover - Python < 3.9
        files = None

    if files is not None:
        try:
            res = files("dataremoval") / "data" / CATALOG_NAME
            if res.is_file():
                return Path(str(res))
        except (ModuleNotFoundError, TypeError, FileNotFoundError):
            pass

    # Zip-safe installs and odd loaders: fall back to the module's own directory.
    local = Path(__file__).resolve().parent / "data" / CATALOG_NAME
    if local.is_file():
        return local

    raise FileNotFoundError(
        "Bundled broker catalog not found. Reinstall the package, or pass "
        "`dr init --catalog /path/to/brokers.yaml`."
    )


def _csv(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    return ",".join(str(v) for v in value)


def load_yaml(path: Path) -> List[Dict[str, Any]]:
    data = yaml.safe_load(path.read_text()) or {}
    brokers = data.get("brokers") or []
    seen = set()
    for b in brokers:
        key = b.get("key")
        if not key:
            raise ValueError(f"catalog entry without a key: {b.get('name', b)}")
        if key in seen:
            raise ValueError(f"duplicate broker key: {key}")
        seen.add(key)
    return brokers


def sync(conn: sqlite3.Connection, brokers: List[Dict[str, Any]]) -> Dict[str, int]:
    """Insert new brokers, refresh catalog fields on existing ones.

    Deliberately does NOT touch url_verified or active: those are local state the
    user earned by checking, and a catalog refresh should not undo it.
    """
    added = updated = 0
    for b in brokers:
        row = conn.execute("SELECT key FROM broker WHERE key = ?", (b["key"],)).fetchone()
        values = (
            b["key"],
            b.get("name", b["key"]),
            int(b.get("tier", 2)),
            b.get("method", "form"),
            b.get("optout_url"),
            b.get("email"),
            _csv(b.get("requires")),
            int(b.get("recheck_days", 180)),
            _csv(b.get("feeds")),
            (b.get("notes") or "").strip(),
        )
        if row is None:
            conn.execute(
                "INSERT INTO broker(key,name,tier,method,optout_url,email,requires,"
                "recheck_days,feeds,notes) VALUES(?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            added += 1
        else:
            conn.execute(
                "UPDATE broker SET name=?,tier=?,method=?,optout_url=?,email=?,requires=?,"
                "recheck_days=?,feeds=?,notes=? WHERE key=?",
                values[1:] + (b["key"],),
            )
            updated += 1
    conn.commit()
    return {"added": added, "updated": updated}

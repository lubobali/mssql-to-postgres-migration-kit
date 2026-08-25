"""
Read schema.json and answer questions about it.

Everything that used to be a hardcoded table name now comes from here, so
pointing the kit at a different database is editing one JSON file rather
than editing SQL in three places and hoping you found them all.

The type is what carries the meaning. It decides how a value is read out
of SQL Server, how it is converted in Python, and which verification
checks apply to it — which is why `money` is a type here and not just
`numeric(19,4)` written somewhere.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "schema.json"

# Types whose totals are compared exactly, never rounded.
MONEY_TYPES = {"money"}

# Types compared by min and max, which is where a timezone shift shows up.
DATE_TYPES = {"timestamp", "date"}

# Computed on the target. Never inserted, only verified.
GENERATED_TYPES = {"generated"}


@functools.lru_cache(maxsize=None)
def load(path: str | None = None) -> dict:
    p = Path(path or os.environ.get("SCHEMA_CONFIG") or DEFAULT_PATH)
    if not p.exists():
        raise FileNotFoundError(f"schema config not found: {p}")

    cfg = json.loads(p.read_text(encoding="utf-8"))
    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    """
    Fail loudly here rather than three files later.

    A typo in a type name would otherwise surface as a column silently
    excluded from every check — which is worse than a crash, because the
    suite would go green over data nobody verified.
    """
    known = MONEY_TYPES | DATE_TYPES | GENERATED_TYPES | {
        "int", "bigint", "text", "char", "bool", "uuid",
    }

    for t in cfg.get("tables", []):
        if "name" not in t or "primary_key" not in t:
            raise ValueError(f"table needs a name and a primary_key: {t}")
        for c in t["columns"]:
            if c["type"] not in known:
                raise ValueError(
                    f"{t['name']}.{c['name']}: unknown type {c['type']!r}. "
                    f"Known types: {', '.join(sorted(known))}"
                )
        pk = t["primary_key"]
        if pk not in [c["name"] for c in t["columns"]]:
            raise ValueError(f"{t['name']}: primary_key {pk!r} is not in columns")


# ─── the questions the rest of the code asks ─────────────────────────


def table_names(cfg: dict) -> list[str]:
    """Parents first. The migration loads in this order and truncates in reverse."""
    return [t["name"] for t in cfg["tables"]]


def table(cfg: dict, name: str) -> dict:
    for t in cfg["tables"]:
        if t["name"] == name:
            return t
    raise KeyError(name)


def columns_of_type(cfg: dict, types: set[str]) -> dict[str, list[str]]:
    return {
        t["name"]: [c["name"] for c in t["columns"] if c["type"] in types]
        for t in cfg["tables"]
        if any(c["type"] in types for c in t["columns"])
    }


def money_columns(cfg: dict) -> dict[str, list[str]]:
    return columns_of_type(cfg, MONEY_TYPES | GENERATED_TYPES)


def date_columns(cfg: dict) -> dict[str, list[str]]:
    return columns_of_type(cfg, DATE_TYPES)


def nullable_columns(cfg: dict) -> dict[str, list[str]]:
    out = {}
    for t in cfg["tables"]:
        cols = [c["name"] for c in t["columns"] if c.get("nullable")]
        if cols:
            out[t["name"]] = cols
    return out


def id_columns(cfg: dict) -> dict[str, str]:
    return {t["name"]: t["primary_key"] for t in cfg["tables"]}


def parent_links(cfg: dict) -> list[tuple[str, str, str]]:
    """(child_table, fk_column, parent_table) for the orphan check."""
    return [
        (t["name"], t["parent"]["column"], t["parent"]["table"])
        for t in cfg["tables"]
        if "parent" in t
    ]


def profiled_parent(cfg: dict) -> tuple[str, str, str] | None:
    """
    The table whose per-parent totals get compared, and its money column.

    A migration can preserve the total row count AND the grand total while
    attaching rows to the wrong parent. Only a per-parent breakdown finds
    that, so the first table with a declared parent is used.
    """
    for t in cfg["tables"]:
        if "parent" not in t:
            continue
        money = [c["name"] for c in t["columns"] if c["type"] in MONEY_TYPES]
        if money:
            return t["name"], t["parent"]["column"], money[0]
    return None


# ─── SQL generation ──────────────────────────────────────────────────


def select_sql(cfg: dict, t: dict) -> str:
    """
    Read every migratable column from SQL Server.

    uuid is cast to VARCHAR deliberately. SQL Server stores the first three
    fields of a UNIQUEIDENTIFIER little-endian while a Postgres uuid is
    big-endian throughout, so moving the raw bytes scrambles the value. The
    string form is identical on both sides.
    """
    src = cfg.get("source_schema", "dbo")
    parts = []
    for c in t["columns"]:
        if c["type"] in GENERATED_TYPES:
            continue
        if c["type"] == "uuid":
            parts.append(f"CAST({c['name']} AS VARCHAR(36))")
        else:
            parts.append(c["name"])
    return (
        f"SELECT {', '.join(parts)} "
        f"FROM {src}.{t['name']} ORDER BY {t['primary_key']}"
    )


def insert_sql(cfg: dict, t: dict) -> str:
    """
    Write to PostgreSQL.

    Generated columns are omitted: Postgres computes them and rejects a
    supplied value.
    """
    cols = [c["name"] for c in t["columns"] if c["type"] not in GENERATED_TYPES]
    placeholders = ", ".join(["%s"] * len(cols))
    return f"INSERT INTO {t['name']} ({', '.join(cols)}) VALUES ({placeholders})"


def migratable_types(t: dict) -> list[str]:
    """Types in SELECT order, so the transform knows what each value is."""
    return [c["type"] for c in t["columns"] if c["type"] not in GENERATED_TYPES]


def qualify(cfg: dict, dialect: str, name: str) -> str:
    """SQL Server needs the schema prefix; Postgres uses the search path."""
    return f"{cfg.get('source_schema', 'dbo')}.{name}" if dialect == "sqlserver" else name


def source_timezone(cfg: dict) -> Any:
    from datetime import timezone

    tz = cfg.get("source_timezone", "UTC")
    if tz.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"unknown source_timezone {tz!r}: {exc}") from exc

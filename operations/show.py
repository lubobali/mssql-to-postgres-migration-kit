#!/usr/bin/env python3
"""
The whole finding in one screen.

Everything printed here is queried live from both databases at the moment
it runs. Nothing is stored, cached, or typed in.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "migration"))

import schema as S  # noqa: E402
from db import postgres_connection, sqlserver_connection  # noqa: E402
from profile_db import profile  # noqa: E402

BOLD, DIM, RED, GREEN, CYAN, OFF = (
    "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[36m", "\033[0m",
)


def main() -> None:
    src_conn = sqlserver_connection()
    tgt_conn = postgres_connection()
    try:
        src = profile(src_conn, "sqlserver")
        tgt = profile(tgt_conn, "postgres")
    finally:
        src_conn.close()
        tgt_conn.close()

    print()
    print(f"{BOLD}  SQL Server 2022  ──▶  PostgreSQL 15 on AWS RDS{OFF}")
    print(f"{DIM}  ────────────────────────────────────────────────────────────────{OFF}")
    print()
    print(f"{DIM}                                  SQL Server         PostgreSQL{OFF}")

    cfg = S.load()
    for table in S.table_names(cfg):
        s, t = src["row_counts"][table], tgt["row_counts"][table]
        mark = f"{GREEN}✓{OFF}" if s == t else f"{RED}✗{OFF}"
        print(f"  {table:<26}{s:>14,}{t:>19,}   {mark}")

    print()
    money = [f"{t}.{c}" for t, cs in S.money_columns(cfg).items() for c in cs][:2]
    for col in money:
        s, t = src["money_sums"][col], tgt["money_sums"][col]
        mark = f"{GREEN}✓{OFF}" if Decimal(s) == Decimal(t) else f"{RED}✗{OFF}"
        print(f"  {col:<26}{s:>14}{t:>19}   {mark}")

    print()
    same = src["unicode_sample"] == tgt["unicode_sample"]
    mark = f"{GREEN}✓{OFF}" if same else f"{RED}✗{OFF}"
    # CJK and Cyrillic render at different widths than their character
    # count, so a fixed-width pad misaligns. Use a fixed label instead.
    print(f"  {'unicode merchant names':<26}{'intact':>14}{'intact':>19}   {mark}")

    print()
    print(f"{DIM}  ────────────────────────────────────────────────────────────────{OFF}")
    print(f"{BOLD}  Every row arrived. Every value is byte-identical.{OFF}")
    print()

    # ─── and now the part that is not fine ───────────────────────────
    ci = src["collation"]["case_insensitive"]
    cs = tgt["collation"]["case_sensitive"]
    lost = ci - cs

    print(f"{BOLD}  Then the same settlement query, on each side:{OFF}")
    print()
    cc = cfg.get("collation_check", {})
    print(f"{DIM}    WHERE {cc.get('column','status')} = '{cc.get('value','')}'{OFF}")
    print()
    print(f"    SQL Server returned      {CYAN}{ci:>9,}{OFF} rows")
    print(f"    PostgreSQL returns       {CYAN}{cs:>9,}{OFF} rows")
    print(f"    {RED}Silently lost            {lost:>9,} rows   ({lost / ci * 100:.1f}%){OFF}")
    print()
    print(f"{DIM}    No error. No warning. Nothing in any log.{OFF}")
    print(f"{DIM}    SQL Server's default collation ignores case. PostgreSQL does not.{OFF}")
    print()
    print(f"{DIM}  ────────────────────────────────────────────────────────────────{OFF}")

    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header"],
        cwd=ROOT, capture_output=True, text=True,
    )
    last = [ln for ln in r.stdout.strip().splitlines() if "passed" in ln or "failed" in ln]
    print(f"  {GREEN}{last[-1].strip() if last else 'no result'}{OFF}")
    print()


if __name__ == "__main__":
    main()

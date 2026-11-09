"""Migration preflight and post-migration verification (#95 release hardening).

    python -m agora_tracker.preflight                 # read-only: graph + live database checks
    python -m agora_tracker.preflight --graph-only    # no database (CI / review)
    python -m agora_tracker.preflight --validate-fill-evidence   # owner role; only if 0 violations

Checks:
* the migration graph is one linear chain with unique, contiguous revision ids whose file name
  prefix matches the revision (a second `0011_*` — e.g. the paused clustering branch — fails here);
* the database revision vs the code head (behind / at / ahead-or-unknown);
* before 0016: verified fills that will be grandfathered by the NOT VALID constraint (reported);
* after 0016: `ck_fill_verified_evidence` state, rows that would violate it (must be 0 before it may
  be VALIDATED), the chain_evidence append-only triggers, RLS and trading_rw grants.
Read-only unless `--validate-fill-evidence`, which runs `ALTER TABLE ... VALIDATE CONSTRAINT` only
when there are zero violations (a VALIDATE never rewrites rows; it scans with a SHARE UPDATE
EXCLUSIVE lock, so run it in a quiet window).
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

VERSIONS = Path(__file__).parent / "migrations" / "versions"
S = "trading_private"


@dataclass
class Item:
    name: str
    status: str            # PASS | FAIL | WARN | INFO
    detail: str


def graph(versions: Path = VERSIONS) -> tuple[list[Item], str | None]:
    out: list[Item] = []
    revs: dict[str, str | None] = {}
    files: dict[str, str] = {}
    problems = []
    for f in sorted(versions.glob("[0-9]*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        vals: dict[str, str | None] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) \
                    and node.targets[0].id in ("revision", "down_revision") \
                    and isinstance(node.value, ast.Constant):
                v = node.value.value
                vals[node.targets[0].id] = v if isinstance(v, str) else None
        rev = vals.get("revision")
        if not isinstance(rev, str):
            problems.append(f"{f.name}: no revision")
            continue
        if rev in revs:
            problems.append(f"duplicate revision {rev}: {files[rev]} and {f.name}")
        if not f.name.startswith(rev + "_"):
            problems.append(f"{f.name}: file prefix does not match revision {rev}")
        revs[rev], files[rev] = vals.get("down_revision"), f.name
    parents = [p for p in revs.values() if p is not None]
    heads = sorted(r for r in revs if r not in parents)
    roots = sorted(r for r, p in revs.items() if p is None)
    if len(heads) != 1:
        problems.append(f"{len(heads)} heads: {heads}")
    if len(roots) != 1:
        problems.append(f"{len(roots)} roots: {roots}")
    for r, p in revs.items():
        if p is not None and p not in revs:
            problems.append(f"{r} points at missing parent {p}")
        if p is not None and re.fullmatch(r"\d{4}", r) and re.fullmatch(r"\d{4}", p) \
                and int(r) != int(p) + 1:
            problems.append(f"{r} -> {p} is not contiguous")
    dup_parents = sorted({p for p in parents if parents.count(p) > 1})
    if dup_parents:
        problems.append(f"branching at {dup_parents}")
    head = heads[0] if len(heads) == 1 else None
    out.append(Item("migration_graph", "FAIL" if problems else "PASS",
                    "; ".join(problems) if problems else f"{len(revs)} revisions, linear, head {head}"))
    return out, head


def database(url: str, head: str | None, *, validate: bool = False) -> list[Item]:
    import sqlalchemy as sa
    out: list[Item] = []
    eng = sa.create_engine(url, pool_pre_ping=True)
    try:
        with eng.connect() as c:
            rev = c.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            st = "PASS" if rev == head else ("WARN" if rev and head and rev < head else "FAIL")
            out.append(Item("database_revision", st, f"database at {rev}, code head {head}"))
            have_fills = c.execute(sa.text("SELECT to_regclass(:t)"),
                                   {"t": f"{S}.order_fills"}).scalar() is not None
            have_ev = c.execute(sa.text("SELECT to_regclass(:t)"),
                                {"t": f"{S}.chain_evidence"}).scalar() is not None
            if have_fills and not have_ev:
                n = c.execute(sa.text(f"SELECT count(*) FROM {S}.order_fills "
                                      "WHERE verdict = 'verified'")).scalar()
                out.append(Item("pre_0016_verified_fills", "INFO" if n == 0 else "WARN",
                                f"{n} verified fill(s) predate corroborated evidence; 0016 keeps them "
                                "(NOT VALID) and they can never be validated — review them"))
            if have_ev:
                row = c.execute(sa.text(
                    "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_fill_verified_evidence'"
                )).first()
                bad = c.execute(sa.text(f"SELECT count(*) FROM {S}.order_fills "
                                        "WHERE verdict = 'verified' AND evidence_id IS NULL")).scalar()
                if row is None:
                    out.append(Item("fill_evidence_constraint", "FAIL", "constraint missing"))
                else:
                    out.append(Item("fill_evidence_constraint", "PASS" if row[0] else "INFO",
                                    "validated" if row[0] else
                                    f"NOT VALID (enforced for new rows); {bad} legacy row(s) violate it"))
                out.append(Item("fill_evidence_violations", "PASS" if bad == 0 else "WARN",
                                f"{bad} verified fill(s) without corroborated evidence"))
                trig = {r[0] for r in c.execute(sa.text(
                    "SELECT tgname FROM pg_trigger WHERE tgrelid = CAST(:t AS regclass) AND NOT tgisinternal"),
                    {"t": f"{S}.chain_evidence"})}
                need = {"chain_evidence_append_only", "chain_evidence_no_truncate"}
                out.append(Item("chain_evidence_append_only", "PASS" if need <= trig else "FAIL",
                                "triggers present" if need <= trig else f"missing {need - trig}"))
                rls = c.execute(sa.text("SELECT relrowsecurity FROM pg_class WHERE oid = CAST(:t AS regclass)"),
                                {"t": f"{S}.chain_evidence"}).scalar()
                out.append(Item("chain_evidence_rls", "PASS" if rls else "FAIL", f"rls={rls}"))
                grants = {r[0] for r in c.execute(sa.text(
                    "SELECT privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_schema = :s AND table_name = 'chain_evidence' AND grantee = 'trading_rw'"),
                    {"s": S})}
                out.append(Item("chain_evidence_grants", "PASS" if grants == {"SELECT", "INSERT"}
                                else "FAIL", f"trading_rw: {sorted(grants)}"))
                if validate:
                    if row is not None and not row[0] and bad == 0:
                        c.execute(sa.text(f"ALTER TABLE {S}.order_fills "
                                          "VALIDATE CONSTRAINT ck_fill_verified_evidence"))
                        c.commit()
                        out.append(Item("validate_fill_evidence", "PASS", "constraint validated"))
                    else:
                        out.append(Item("validate_fill_evidence", "FAIL" if bad else "INFO",
                                        "refused: violations exist" if bad else "nothing to do"))
    finally:
        eng.dispose()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="migration preflight / verification")
    ap.add_argument("--graph-only", action="store_true")
    ap.add_argument("--database-url-env", default="DATABASE_URL",
                    help="env var with the OWNER database URL (never passed on the command line)")
    ap.add_argument("--validate-fill-evidence", action="store_true")
    a = ap.parse_args(argv)
    items, head = graph()
    if not a.graph_only:
        url = os.environ.get(a.database_url_env, "")
        if not url:
            items.append(Item("database", "FAIL", f"{a.database_url_env} is not set"))
        else:
            try:
                items += database(url, head, validate=a.validate_fill_evidence)
            except Exception as exc:                                  # noqa: BLE001
                items.append(Item("database", "FAIL", f"cannot inspect ({type(exc).__name__})"))
    for i in items:
        print(f"{i.status:4}  {i.name:30} {i.detail}")
    return 1 if any(i.status == "FAIL" for i in items) else 0


if __name__ == "__main__":
    sys.exit(main())

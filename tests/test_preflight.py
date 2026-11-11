"""Migration preflight (#95 release hardening): the graph must be one linear chain (a second
0011, as on the paused clustering branch, fails), and the post-0016 checks see the real schema."""

from __future__ import annotations

import shutil

import pytest
from conftest import TEST_URL

from agora_tracker import preflight as P


def test_the_shipped_migration_graph_is_linear_and_contiguous():
    items, head = P.graph()
    assert items[0].status == "PASS", items[0].detail
    assert head == "0016"


def copy_versions(tmp_path):
    d = tmp_path / "versions"
    shutil.copytree(P.VERSIONS, d, ignore=shutil.ignore_patterns("__pycache__"))
    return d


def test_a_conflicting_0011_like_the_clustering_branch_fails(tmp_path):
    d = copy_versions(tmp_path)
    (d / "0011_wallet_clusters.py").write_text('revision = "0011"\ndown_revision = "0010"\n')
    items, head = P.graph(d)
    assert items[0].status == "FAIL" and "duplicate revision 0011" in items[0].detail


def test_the_renumbered_clustering_migration_0017_on_0016_passes(tmp_path):
    d = copy_versions(tmp_path)
    (d / "0017_wallet_clusters.py").write_text('revision = "0017"\ndown_revision = "0016"\n')
    items, head = P.graph(d)
    assert items[0].status == "PASS" and head == "0017"


@pytest.mark.parametrize("name,body,why", [
    ("0017_x.py", 'revision = "0017"\ndown_revision = "0015"\n', "branching"),
    ("0018_x.py", 'revision = "0018"\ndown_revision = "0016"\n', "not contiguous"),
    ("0017_x.py", 'revision = "0018"\ndown_revision = "0016"\n', "prefix"),
    ("0017_x.py", 'revision = "0017"\ndown_revision = "0099"\n', "missing parent"),
])
def test_graph_defects_are_reported(tmp_path, name, body, why):
    d = copy_versions(tmp_path)
    (d / name).write_text(body)
    items, _ = P.graph(d)
    assert items[0].status == "FAIL" and why in items[0].detail


@pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")
def test_post_0016_database_checks_pass_on_a_migrated_database(pg_engine):
    items = {i.name: i for i in P.database(TEST_URL, "0016")}
    assert items["database_revision"].status == "PASS"
    assert items["fill_evidence_constraint"].status in ("INFO", "PASS")
    assert items["fill_evidence_violations"].status == "PASS"
    assert items["chain_evidence_append_only"].status == "PASS"
    assert items["chain_evidence_rls"].status == "PASS"
    assert items["chain_evidence_grants"].status == "PASS"


@pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")
def test_validate_only_runs_with_zero_violations(pg_engine):
    from test_trading_ledger import raw
    items = {i.name: i for i in P.database(TEST_URL, "0016", validate=True)}
    assert items["validate_fill_evidence"].status in ("PASS", "INFO")
    assert raw(pg_engine, "SELECT convalidated FROM pg_constraint "
                          "WHERE conname = 'ck_fill_verified_evidence'")[0][0] is True
    # put the database back the way migrations leave it (NOT VALID) for the other tests
    raw(pg_engine, "ALTER TABLE trading_private.order_fills DROP CONSTRAINT ck_fill_verified_evidence")
    raw(pg_engine, "ALTER TABLE trading_private.order_fills ADD CONSTRAINT ck_fill_verified_evidence "
                   "CHECK (verdict <> 'verified' OR evidence_id IS NOT NULL) NOT VALID")


def test_cli_graph_only(capsys):
    assert P.main(["--graph-only"]) == 0
    assert "migration_graph" in capsys.readouterr().out

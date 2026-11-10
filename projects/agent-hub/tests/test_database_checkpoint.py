"""Inventory comparisons reject lost data, weakened structure and unsafe identifiers."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "operator_checkpoint", Path(__file__).resolve().parents[1] / "deploy/database-checkpoint.py"
)
checkpoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkpoint)


def inventory():
    return {
        "schema": 1,
        "database_schema": "0017",
        "structure_sha256": "a" * 64,
        "tables": [{"name": "worklogs", "columns": ["id", "notes"], "rows": 4, "sha256": "b" * 64}],
    }


def test_restore_requires_both_exact_contents_and_structural_protection():
    source = inventory()
    result = checkpoint.compare(source, deepcopy(source))
    assert result["passed"] and result["structure_verified"] and result["verified_rows"] == 4
    for modify in (
        lambda v: v["tables"][0].update(rows=3),
        lambda v: v["tables"][0].update(sha256="c" * 64),
        lambda v: v.update(structure_sha256="d" * 64),
        lambda v: v.update(database_schema="0024"),
    ):
        restored = deepcopy(source)
        modify(restored)
        with pytest.raises(checkpoint.CheckpointError):
            checkpoint.compare(source, restored)


def test_upgrade_can_extend_schema_but_cannot_change_existing_column_values():
    source, upgraded = inventory(), inventory()
    upgraded.update(database_schema="0024", structure_sha256="d" * 64)
    report = checkpoint.compare(source, upgraded, "upgrade")
    assert report["passed"] and not report["structure_verified"]
    upgraded["tables"][0]["sha256"] = "c" * 64
    with pytest.raises(checkpoint.CheckpointError, match="RESTORED_DATA_MISMATCH"):
        checkpoint.compare(source, upgraded, "upgrade")


def test_untrusted_inventory_cannot_supply_sql_identifiers_or_shell_targets():
    for name in ("worklogs; DELETE FROM users", "../users", 'users"', "", "x" * 64):
        value = inventory()
        value["tables"][0]["name"] = name
        with pytest.raises(checkpoint.CheckpointError):
            checkpoint.baseline(value)
    value = inventory()
    value["tables"][0]["columns"].append("(SELECT password_hash FROM users)")
    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.baseline(value)
    for container, database in (("--privileged", "hub_test"), ("agent-hub-test", "postgres")):
        with pytest.raises(checkpoint.CheckpointError):
            checkpoint.Database(container, database)


def test_actual_restore_timestamp_is_allowed_without_opening_arbitrary_databases():
    for database in (
        "agent_hub",
        "hub_test",
        "hub_restore_20261110T004611Z",
        "hub_restore_snapshot_123",
        "hub_upgrade_snapshot_123",
    ):
        assert checkpoint.Database("agent-hub-complete-test-db", database).database == database
    for database in (
        "hub_restore_20261110T004611Z;DROP DATABASE agent_hub",
        "hub_restore_20261110T004611Z_extra",
        "hub_restore_20261110X004611Z",
        "hub_upgrade_20261110T004611Z",
        "agent_hub_other",
        "hub_upgrade_" + "a" * 51,
    ):
        with pytest.raises(checkpoint.CheckpointError, match="INVALID_DATABASE_NAME"):
            checkpoint.Database("agent-hub-complete-test-db", database)


def test_upgrade_only_exempts_the_migration_version_record():
    source = inventory()
    source["tables"].append(
        {"name": "alembic_version", "columns": ["version_num"], "rows": 1, "sha256": "c" * 64}
    )
    upgraded = deepcopy(source)
    upgraded.update(database_schema="0024", structure_sha256="d" * 64)
    upgraded["tables"][1]["sha256"] = "e" * 64
    result = checkpoint.compare(source, upgraded, "upgrade")
    assert result["passed"] and result["verified_tables"] == 1 and result["verified_rows"] == 4
    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.compare(source, upgraded, "exact")
    upgraded["tables"][0]["sha256"] = "f" * 64
    with pytest.raises(checkpoint.CheckpointError, match="RESTORED_DATA_MISMATCH"):
        checkpoint.compare(source, upgraded, "upgrade")


def test_pg_dump_equivalent_array_casts_have_the_same_fingerprint():
    for original, restored in (
        (
            "CHECK (state = ANY (ARRAY['pending'::character varying, 'it''s'::character varying]::text[]))",
            "CHECK (state = ANY (ARRAY['pending'::character varying::text, "
            "'it''s'::character varying::text]))",
        ),
        (
            "WHERE (state = ANY ((ARRAY['pending'::character varying])::text[]))",
            "WHERE (state = ANY (ARRAY[('pending'::character varying)::text]))",
        ),
    ):
        assert checkpoint.canonical_sql(original) == checkpoint.canonical_sql(restored)
        assert checkpoint.canonical_sql(original) != checkpoint.canonical_sql(
            restored.replace("pending", "other")
        )
        for kind in ("constraint", "index", "policy", "trigger", "column"):

            def record(sql, kind=kind):
                fields = {
                    "constraint": sql,
                    "index": sql,
                    "policy": json.dumps(["PERMISSIVE", ["hub_runtime"], "UPDATE", sql, sql]),
                    "trigger": json.dumps(["O", sql]),
                    "column": json.dumps(["text[]", True, sql, "", ""]),
                }
                return json.dumps({"kind": kind, "name": "test", "definition": fields[kind]})

            assert checkpoint.structure_record(record(original)) == checkpoint.structure_record(
                record(restored)
            )


def test_sql_fingerprint_preserves_literals_types_and_protection_changes():
    for value in (
        "ARRAY['x'::character varying(3)]::text[]",
        "ARRAY['x'::character varying]::integer[]",
        "ARRAY['x'::text]::character varying[]",
        "'ARRAY[''x''::character varying]::text[]'",
        "\"ARRAY['x'::character varying]::text[]\"",
        "E'ARRAY[\\'x\\'::character varying]::text[]'",
    ):
        assert checkpoint.canonical_sql(value) == value

    def table(rls):
        return json.dumps(
            {"kind": "table", "name": "worklogs", "definition": json.dumps([rls, True, "hub_owner", []])}
        )

    assert checkpoint.structure_record(table(True)) != checkpoint.structure_record(table(False))


def test_duplicate_or_unbounded_inventory_is_never_accepted_as_recovery_proof():
    for change in (
        lambda v: v["tables"].append(deepcopy(v["tables"][0])),
        lambda v: v["tables"][0]["columns"].append("id"),
        lambda v: v["tables"][0].update(rows=True),
        lambda v: v["tables"][0].update(sha256="not-a-digest"),
    ):
        value = inventory()
        change(value)
        with pytest.raises(checkpoint.CheckpointError):
            checkpoint.baseline(value)

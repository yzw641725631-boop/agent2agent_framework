#!/usr/bin/env python3
# ruff: noqa: UP017
"""Operator-only consistent dump and hashed inventory. No row contents enter the report.

Uses the server's PostgreSQL container and standard library Python 3.6+. The exported
read-only transaction stays open until the dump and all inventory reads finish.
"""

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import re
import select
import subprocess
import sys
from pathlib import Path


class CheckpointError(Exception):
    pass


IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SNAPSHOT = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9]+$")
CATALOG = """
SELECT json_build_object('name',c.relname,'columns',(
  SELECT json_agg(a.attname ORDER BY a.attnum) FROM pg_attribute a
  WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped))
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY c.relname
"""
STRUCTURE = """
SELECT row_to_json(evidence)::text FROM (
  SELECT 'column' AS kind,n.nspname||'.'||c.relname||'.'||a.attname AS name,
    json_build_array(format_type(a.atttypid,a.atttypmod),a.attnotnull,
      pg_get_expr(d.adbin,d.adrelid),a.attidentity,a.attgenerated)::text AS definition
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
    LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
  WHERE n.nspname='public' AND c.relkind IN ('r','p')
  UNION ALL
  SELECT 'table',n.nspname||'.'||c.relname,
    json_build_array(c.relrowsecurity,c.relforcerowsecurity,pg_get_userbyid(c.relowner),(
      SELECT jsonb_agg(jsonb_build_array(pg_get_userbyid(grantor),
        CASE WHEN grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(grantee)::text END,
        privilege_type,is_grantable) ORDER BY pg_get_userbyid(grantor)::text,
        CASE WHEN grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(grantee)::text END,
        privilege_type,is_grantable)
      FROM aclexplode(coalesce(c.relacl,acldefault('r',c.relowner)))))::text
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='public' AND c.relkind IN ('r','p')
  UNION ALL
  SELECT 'constraint',conrelid::regclass::text||'.'||conname,pg_get_constraintdef(oid,true)
  FROM pg_constraint WHERE connamespace='public'::regnamespace
  UNION ALL
  SELECT 'policy',schemaname||'.'||tablename||'.'||policyname,
    json_build_array(permissive,roles,cmd,qual,with_check)::text FROM pg_policies
  WHERE schemaname='public'
  UNION ALL
  SELECT 'index',schemaname||'.'||indexname,indexdef FROM pg_indexes WHERE schemaname='public'
  UNION ALL
  SELECT 'trigger',tgrelid::regclass::text||'.'||tgname,
    json_build_array(tgenabled,pg_get_triggerdef(oid,true))::text
  FROM pg_trigger WHERE NOT tgisinternal AND tgrelid IN (
    SELECT c.oid FROM pg_class c WHERE c.relnamespace='public'::regnamespace)
  UNION ALL
  SELECT 'function',n.nspname||'.'||p.proname||'('||pg_get_function_identity_arguments(p.oid)||')',
    json_build_array(pg_get_functiondef(p.oid),pg_get_userbyid(p.proowner),(
      SELECT jsonb_agg(jsonb_build_array(pg_get_userbyid(grantor),
        CASE WHEN grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(grantee)::text END,
        privilege_type,is_grantable) ORDER BY pg_get_userbyid(grantor)::text,
        CASE WHEN grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(grantee)::text END,
        privilege_type,is_grantable)
      FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner)))))::text
  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname IN ('public','hub') AND p.prokind IN ('f','p')
) evidence ORDER BY kind,name
"""

# pg_dump reparses varchar-literal arrays coerced to text[] as arrays of text
# elements. Normalize only that proven identity, without changing arbitrary SQL,
# casts with length limits, quoted identifiers or the contents of string literals.
SQL_LITERAL = r"'(?:[^'\\]|'')*'"
VARCHAR_LITERAL = SQL_LITERAL + r"::character varying"
ARRAY_VALUES = VARCHAR_LITERAL + r"(?:, " + VARCHAR_LITERAL + r")*"
ARRAY_COERCION = re.compile(
    r"(?:\(ARRAY\[(?P<wrapped>" + ARRAY_VALUES + r")\]\)|ARRAY\[(?P<plain>" + ARRAY_VALUES + r")\])::text\[\]"
)
TEXT_ELEMENT = r"(?:\(" + VARCHAR_LITERAL + r"\)|" + VARCHAR_LITERAL + r")::text"
TEXT_ARRAY = re.compile(r"ARRAY\[(" + TEXT_ELEMENT + r"(?:, " + TEXT_ELEMENT + r")*)\]")
QUOTED_SQL = re.compile(r"[eE]'(?:[^'\\]|\\.|'')*'|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")


def canonical_sql(value):
    result, offset = [], 0
    while offset < len(value):
        quoted = QUOTED_SQL.match(value, offset)
        if quoted:
            result.append(quoted.group())
            offset = quoted.end()
            continue
        array = ARRAY_COERCION.match(value, offset) or TEXT_ARRAY.match(value, offset)
        if array and (not offset or not re.match(r"[a-zA-Z0-9_$]", value[offset - 1])):
            literals = re.findall(SQL_LITERAL, array.group())
            result.append("ARRAY[" + ", ".join(literal + "::text" for literal in literals) + "]")
            offset = array.end()
            continue
        result.append(value[offset])
        offset += 1
    return "".join(result)


def structure_record(row):
    value = json.loads(row)
    kind, definition = value["kind"], value["definition"]
    if kind in ("constraint", "index"):
        value["definition"] = canonical_sql(definition)
    elif kind in ("policy", "trigger", "column"):
        fields = json.loads(definition)
        positions = {"policy": (3, 4), "trigger": (1,), "column": (2,)}[kind]
        for position in positions:
            if fields[position] is not None:
                fields[position] = canonical_sql(fields[position])
        value["definition"] = fields
    else:
        value["definition"] = json.loads(definition)
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise CheckpointError("INVALID_INVENTORY_IDENTIFIER")
    return '"' + value + '"'


def baseline(value):
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise CheckpointError("INVALID_BASELINE")
    tables = value.get("tables")
    if not isinstance(tables, list) or not 1 <= len(tables) <= 1000:
        raise CheckpointError("INVALID_BASELINE")
    names = set()
    for table in tables:
        identifier(table.get("name"))
        columns = table.get("columns")
        if table["name"] in names or not isinstance(columns, list) or not 1 <= len(columns) <= 1000:
            raise CheckpointError("INVALID_BASELINE")
        names.add(table["name"])
        for column in columns:
            identifier(column)
        if len(set(columns)) != len(columns) or type(table.get("rows")) is not int or table["rows"] < 0:
            raise CheckpointError("INVALID_BASELINE")
        if not re.fullmatch(r"[a-f0-9]{64}", table.get("sha256", "")):
            raise CheckpointError("INVALID_BASELINE")
    if not re.fullmatch(r"[a-f0-9]{64}", value.get("structure_sha256", "")):
        raise CheckpointError("INVALID_BASELINE")
    if not re.fullmatch(r"[0-9]{4}", value.get("database_schema", "")):
        raise CheckpointError("INVALID_BASELINE")
    return value


class Database:
    def __init__(self, container, database):
        if not re.fullmatch(r"agent-hub-[a-z0-9-]{1,100}", container):
            raise CheckpointError("INVALID_DATABASE_CONTAINER")
        if not re.fullmatch(
            r"agent_hub|hub_test|hub_(?:restore|upgrade)_[a-z0-9_]{1,50}"
            r"|hub_restore_[0-9]{8}T[0-9]{6}Z",
            database,
        ):
            raise CheckpointError("INVALID_DATABASE_NAME")
        self.container, self.database = container, database
        self.command = [
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-X",
            "-qAt",
            "-U",
            "postgres",
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
        ]

    @contextlib.contextmanager
    def snapshot(self):
        with open(os.devnull, "wb") as errors:
            process = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors
            )
        try:
            process.stdin.write(
                b"BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SELECT pg_export_snapshot();\n"
            )
            process.stdin.flush()
            readable, _, _ = select.select([process.stdout], [], [], 30)
            token = process.stdout.readline(100).decode().strip() if readable else ""
            if not SNAPSHOT.fullmatch(token):
                raise CheckpointError("DATABASE_SNAPSHOT_UNAVAILABLE")
            yield token
            process.stdin.write(b"ROLLBACK;\n\\q\n")
            process.stdin.flush()
            if process.wait(timeout=10):
                raise CheckpointError("DATABASE_SNAPSHOT_FAILED")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdin.close()
            process.stdout.close()

    def rows(self, sql, snapshot):
        if not SNAPSHOT.fullmatch(snapshot):
            raise CheckpointError("INVALID_DATABASE_SNAPSHOT")
        # Identifiers come only from the server catalog or a strictly validated baseline.
        statement = (
            "SET statement_timeout='300s'; SET timezone='UTC'; SET datestyle='ISO,YMD'; "
            "SET search_path=pg_catalog,public; BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; "
        )
        statement += "SET TRANSACTION SNAPSHOT '" + snapshot + "'; " + sql + "; ROLLBACK;"
        with open(os.devnull, "wb") as errors:
            process = subprocess.Popen(
                self.command + ["-c", statement], stdout=subprocess.PIPE, stderr=errors
            )
        try:
            for line in process.stdout:
                if len(line) > 1024 * 1024:
                    raise CheckpointError("OVERSIZED_DATABASE_METADATA")
                yield line.rstrip(b"\r\n")
            if process.wait(timeout=10):
                raise CheckpointError("DATABASE_INVENTORY_FAILED")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()

    def inventory(self, snapshot, projection=None):
        tables = [json.loads(row) for row in self.rows(CATALOG, snapshot)]
        available = {table["name"]: table["columns"] for table in tables}
        if projection:
            for table in projection["tables"]:
                if table["name"] not in available or not set(table["columns"]).issubset(
                    available[table["name"]]
                ):
                    raise CheckpointError("BASELINE_SCHEMA_REMOVED")
            tables = projection["tables"]
        result = []
        for table in tables:
            name = identifier(table["name"])
            columns = ",".join(identifier(column) for column in table["columns"])
            sql = "SELECT encode(sha256(convert_to(to_jsonb(record)::text,'UTF8')),'hex') AS hash "
            sql += "FROM (SELECT " + columns + " FROM public." + name + ") record ORDER BY hash"
            digest, count = hashlib.sha256(), 0
            for row in self.rows(sql, snapshot):
                if not re.fullmatch(b"[a-f0-9]{64}", row):
                    raise CheckpointError("INVALID_ROW_DIGEST")
                digest.update(row + b"\n")
                count += 1
            result.append(
                {
                    "name": table["name"],
                    "columns": table["columns"],
                    "rows": count,
                    "sha256": digest.hexdigest(),
                }
            )
        structure = hashlib.sha256()
        for row in self.rows(STRUCTURE, snapshot):
            structure.update(structure_record(row) + b"\n")
        versions = list(self.rows("SELECT version_num FROM public.alembic_version", snapshot))
        if len(versions) != 1 or not re.fullmatch(b"[0-9]{4}", versions[0]):
            raise CheckpointError("INVALID_DATABASE_REVISION")
        return {
            "schema": 1,
            "database_schema": versions[0].decode(),
            "structure_sha256": structure.hexdigest(),
            "tables": result,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    def dump(self, path, snapshot):
        with path.open("xb") as output, open(os.devnull, "wb") as errors:
            process = subprocess.run(
                [
                    "docker",
                    "exec",
                    self.container,
                    "pg_dump",
                    "-U",
                    "postgres",
                    "-d",
                    self.database,
                    "-Fc",
                    "--snapshot=" + snapshot,
                ],
                stdout=output,
                stderr=errors,
                timeout=900,
            )
            output.flush()
            os.fsync(output.fileno())
        if process.returncode:
            raise CheckpointError("DATABASE_DUMP_FAILED")


def compare(expected, actual, mode="exact"):
    baseline(expected)
    baseline(actual)
    before = {table["name"]: table for table in expected["tables"]}
    after = {table["name"]: table for table in actual["tables"]}
    # Alembic's single version row must change during a schema upgrade. It remains
    # fully covered by exact restore verification and revision validation above.
    verified = {
        name: table for name, table in before.items() if mode != "upgrade" or name != "alembic_version"
    }
    changed = [name for name, table in verified.items() if after.get(name) != table]
    if mode == "exact" and (
        set(before) != set(after)
        or expected["structure_sha256"] != actual["structure_sha256"]
        or expected["database_schema"] != actual["database_schema"]
    ):
        raise CheckpointError("RESTORED_SCHEMA_MISMATCH")
    if changed:
        raise CheckpointError("RESTORED_DATA_MISMATCH")
    return {
        "passed": True,
        "mode": mode,
        "database_schema": actual["database_schema"],
        "verified_tables": len(verified),
        "verified_rows": sum(t["rows"] for t in verified.values()),
        "structure_verified": mode == "exact",
        "business_contents_disclosed": False,
    }


def write(path, value):
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["capture", "inventory", "verify"])
    parser.add_argument("--container", default="agent-hub-postgres")
    parser.add_argument("--database", default="agent_hub")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--mode", choices=["exact", "upgrade"], default="exact")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise CheckpointError("SERVER_OPERATOR_REQUIRED")
    os.umask(0o077)
    connection = Database(args.container, args.database)
    expected = None
    if args.baseline:
        if args.baseline.is_symlink() or args.baseline.stat().st_size > 4 * 1024 * 1024:
            raise CheckpointError("INVALID_BASELINE_FILE")
        expected = baseline(json.loads(args.baseline.read_text(encoding="utf-8")))
    if args.operation == "verify" and expected is None:
        raise CheckpointError("BASELINE_REQUIRED")
    with connection.snapshot() as snapshot:
        if args.operation == "capture":
            if args.baseline:
                raise CheckpointError("CAPTURE_BASELINE_NOT_APPLICABLE")
            args.output.mkdir(mode=0o700)
            connection.dump(args.output / "database.dump", snapshot)
            inventory = connection.inventory(snapshot)
            write(args.output / "inventory.json", inventory)
            result = {
                "passed": True,
                "consistent_snapshot": True,
                "database_schema": inventory["database_schema"],
                "tables": len(inventory["tables"]),
                "rows": sum(t["rows"] for t in inventory["tables"]),
            }
        else:
            inventory = connection.inventory(snapshot, expected if args.mode == "upgrade" else None)
            result = compare(expected, inventory, args.mode) if expected else inventory
            write(args.output, result)
    print(
        json.dumps(
            result
            if args.operation != "inventory"
            else {
                "passed": True,
                "database_schema": result["database_schema"],
                "tables": len(result["tables"]),
                "rows": sum(t["rows"] for t in result["tables"]),
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # PostgreSQL diagnostics and command output may contain credentials or original data.
        code = str(error) if isinstance(error, CheckpointError) else "CHECKPOINT_FAILED"
        sys.stderr.write(code + "\n")
        sys.exit(1)

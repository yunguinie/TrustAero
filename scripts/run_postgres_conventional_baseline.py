"""Run the frozen PostgreSQL conventional-governance baseline."""
from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CONTAINER = "trustaero-postgres-baseline"
PASSWORD = "benchmark-only"
LATENCY = re.compile(r"latency average = ([0-9.]+) ms")


def command(args: list[str], *, stdin: str | None = None, check: bool = True) -> str:
    result = subprocess.run(
        args,
        input=stdin,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        command_text = " ".join(args)
        raise RuntimeError(
            f"Command failed ({result.returncode}): {command_text}\n{result.stderr}"
        )
    return result.stdout


def docker(executable: str, *args: str, stdin: str | None = None, check: bool = True) -> str:
    return command([executable, *args], stdin=stdin, check=check)


def psql(docker_exe: str, user: str, sql: str) -> str:
    args = [
        "exec",
        "-e",
        f"PGPASSWORD={PASSWORD}",
        "-i",
        CONTAINER,
        "psql",
        "-h",
        "127.0.0.1",
        "-U",
        user,
        "-d",
        "postgres",
        "-qAt",
        "-v",
        "ON_ERROR_STOP=1",
    ]
    return docker(docker_exe, *args, stdin=sql).strip()


def summary_sql(source: str, direct: bool = False) -> str:
    settings = "" if direct else (
        "SET ta.tenant='3'; SET ta.purpose='research'; "
        "SET ta.can_view_sensitive='false';"
    )
    tenant = "e.tenant_id=3 AND" if direct else ""
    sensitive = "count(e.sensitive_value)" if source == "governed_events" else "0"
    return f"""
{settings}
SELECT json_build_object(
  'rows', count(*),
  'region_sum', sum(e.region_id),
  'magnitude_mean', round(avg(e.magnitude)::numeric, 6),
  'visible_sensitive', {sensitive}
)::text
FROM {source} e JOIN regions r USING(region_id)
WHERE {tenant}
  e.event_time >= DATE '2024-03-01'
  AND e.event_time < DATE '2024-10-01'
  AND e.magnitude >= 3.0;
"""


def pgbench(
    docker_exe: str,
    user: str,
    script: str,
    transactions: int,
) -> tuple[float, str]:
    output = docker(
        docker_exe,
        "exec",
        "-e",
        f"PGPASSWORD={PASSWORD}",
        CONTAINER,
        "pgbench",
        "-h",
        "127.0.0.1",
        "-U",
        user,
        "-d",
        "postgres",
        "-M",
        "simple",
        "-c",
        "1",
        "-j",
        "1",
        "-t",
        str(transactions),
        "-f",
        f"/bench/{script}",
    )
    match = LATENCY.search(output)
    if not match:
        raise RuntimeError(f"Could not parse pgbench latency:\n{output}")
    return float(match.group(1)), output


def node_types(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        if isinstance(value.get("Node Type"), str):
            found.append(value["Node Type"])
        for child in value.values():
            found.extend(node_types(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(node_types(child))
    return found


def run(docker_exe: str, root: Path, output_dir: Path) -> Path:
    protocol_path = root / "experiments/frozen/postgres_conventional_baseline_v1_20260901.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    if protocol.get("frozen") is not True:
        raise RuntimeError("Protocol is not frozen")
    bench_dir = root / "experiments/postgres_conventional"
    output_dir.mkdir(parents=True, exist_ok=False)
    raw: dict[str, Any] = {}
    docker(docker_exe, "rm", "-f", CONTAINER, check=False)
    try:
        docker(
            docker_exe,
            "run",
            "-d",
            "--name",
            CONTAINER,
            "-e",
            f"POSTGRES_PASSWORD={PASSWORD}",
            protocol["postgres_image"],
            "-c",
            "log_statement=all",
            "-c",
            "jit=off",
        )
        for _ in range(60):
            status = docker(
                docker_exe,
                "exec",
                CONTAINER,
                "pg_isready",
                "-U",
                "postgres",
                check=False,
            )
            if "accepting connections" in status:
                break
            time.sleep(1)
        else:
            raise RuntimeError("PostgreSQL did not become ready")
        setup = (bench_dir / "setup.sql").read_text(encoding="utf-8")
        raw["setup"] = docker(
            docker_exe,
            "exec",
            "-i",
            CONTAINER,
            "psql",
            "-h",
            "127.0.0.1",
            "-U",
            "postgres",
            "-d",
            "postgres",
            stdin=setup,
        )
        docker(docker_exe, "cp", f"{bench_dir}{Path('/').anchor}.", f"{CONTAINER}:/bench")
        direct = json.loads(psql(docker_exe, "ta_direct", summary_sql("events", direct=True)))
        rls = json.loads(psql(docker_exe, "ta_rls", summary_sql("events")))
        masked = json.loads(psql(docker_exe, "ta_rls", summary_sql("governed_events")))
        wrong_purpose = int(
            psql(
                docker_exe,
                "ta_rls",
                "SET ta.tenant='3'; SET ta.purpose='marketing'; SELECT count(*) FROM events;",
            )
        )
        cross_tenant = int(
            psql(
                docker_exe,
                "ta_rls",
                "SET ta.tenant='3'; SET ta.purpose='research'; "
                "SELECT count(*) FROM events WHERE tenant_id<>3;",
            )
        )
        psql(
            docker_exe,
            "ta_rls",
            "SET ta.tenant='3'; SET ta.purpose='research'; "
            "SET ta.can_view_sensitive='false'; SELECT count(*) FROM governed_events;",
        )
        masked_sensitive = int(
            psql(
                docker_exe,
                "ta_rls",
                "SET ta.tenant='3'; SET ta.purpose='research'; "
                "SET ta.can_view_sensitive='false'; "
                "SELECT count(*) FROM governed_events WHERE sensitive_value IS NOT NULL;",
            )
        )
        unmasked_sensitive = int(
            psql(
                docker_exe,
                "ta_rls",
                "SET ta.tenant='3'; SET ta.purpose='research'; "
                "SET ta.can_view_sensitive='true'; "
                "SELECT count(*) FROM governed_events WHERE sensitive_value IS NOT NULL;",
            )
        )
        explain_sql = """
SET ta.tenant='3'; SET ta.purpose='research'; SET ta.can_view_sensitive='false';
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
SELECT count(*), sum(e.region_id), round(avg(e.magnitude)::numeric, 6)
FROM governed_events e JOIN regions r USING(region_id)
WHERE e.event_time >= DATE '2024-03-01'
  AND e.event_time < DATE '2024-10-01'
  AND e.magnitude >= 3.0;
"""
        plan_text = psql(docker_exe, "ta_rls", explain_sql)
        plan = json.loads(plan_text)
        measurements: dict[str, list[float]] = {}
        methods = {
            "direct_sql": ("ta_direct", "direct_sql.sql"),
            "rls": ("ta_rls", "rls.sql"),
            "rls_masked_view": ("ta_rls", "rls_masked_view.sql"),
        }
        for method, (user, script) in methods.items():
            _, warmup = pgbench(
                docker_exe, user, script, int(protocol["warmup_transactions"])
            )
            raw[f"{method}_warmup"] = warmup
            values = []
            for block in range(int(protocol["measurement_blocks"])):
                latency, output = pgbench(
                    docker_exe,
                    user,
                    script,
                    int(protocol["transactions_per_block"]),
                )
                values.append(latency)
                raw[f"{method}_block_{block + 1}"] = output
            measurements[method] = values
        log_process = subprocess.run(
            [docker_exe, "logs", "--tail", "200", CONTAINER],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        logs = log_process.stdout + log_process.stderr
        result = {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "protocol": protocol,
            "correctness": {
                "direct_summary": direct,
                "rls_summary": rls,
                "masked_view_summary": masked,
                "result_equivalence": direct == rls == masked,
                "wrong_purpose_visible_rows": wrong_purpose,
                "cross_tenant_rows": cross_tenant,
                "masked_sensitive_values": masked_sensitive,
                "unmasked_sensitive_values": unmasked_sensitive,
            },
            "native_plan": {
                "node_types": node_types(plan),
                "plan": plan,
            },
            "performance": {
                method: {
                    "block_latency_ms": values,
                    "median_latency_ms": statistics.median(values),
                    "minimum_latency_ms": min(values),
                    "maximum_latency_ms": max(values),
                }
                for method, values in measurements.items()
            },
            "statement_log": {
                "tail_line_count": len(logs.splitlines()),
                "contains_query_text": "SELECT count(*)" in logs,
                "contains_result_digest": "result_digest" in logs,
                "contains_row_lineage": "row_lineage" in logs,
                "tail": logs,
            },
            "raw_pgbench": raw,
        }
        path = output_dir / "postgres_conventional_baseline_results.json"
        path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        return path
    finally:
        docker(docker_exe, "rm", "-f", CONTAINER, check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(run(args.docker, args.root.resolve(), args.output.resolve()))


if __name__ == "__main__":
    main()

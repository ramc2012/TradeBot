"""Run the isolated research collector beside an existing TradeBot container.

Uses the existing dependency image, network and encrypted credentials file.
Application code/credentials are mounted read-only. Only the research directory
is writable. Temporary environment material is mode 0600 and deleted immediately.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import shlex


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-container", default="nomadcurie_backend")
    parser.add_argument("--name", default="nomadcurie_edge_research_v1")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    metadata = json.loads(
        subprocess.check_output(["docker", "inspect", args.source_container], text=True)
    )[0]
    env = dict(item.split("=", 1) for item in metadata["Config"]["Env"] if "=" in item)
    credentials = next(
        (
            m["Source"]
            for m in metadata["Mounts"]
            if m["Destination"] == "/app/credentials.json" and m["Type"] == "bind"
        ),
        None,
    )
    if (
        not credentials
        or not Path(credentials).is_file()
        or not env.get("DATABASE_URL")
    ):
        raise SystemExit(
            "Existing container must expose its credentials bind and database connection"
        )
    networks = list(metadata["NetworkSettings"]["Networks"])
    if len(networks) != 1:
        raise SystemExit("Choose a source container with one unambiguous network")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    chosen = {
        k: env[k] for k in ("DATABASE_URL", "REDIS_URL", "SECRET_KEY") if k in env
    }
    chosen.update(
        APP_ENV="production",
        PAPER_TRADING_ONLY="true",
        PAPER_APPLY_COSTS="true",
        CREDENTIALS_FILE="/credentials.json",
        PYTHONDONTWRITEBYTECODE="1",
        OPENBLAS_NUM_THREADS="1",
    )
    with tempfile.TemporaryDirectory(prefix="edge-research-env-") as temp:
        env_file = Path(temp) / "env"
        env_file.write_text("\n".join(f"{k}={v}" for k, v in chosen.items()) + "\n")
        env_file.chmod(0o600)
        command = [
            "docker",
            "run",
            "--name",
            args.name,
            "--entrypoint",
            "python",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            "0.25",
            "--memory",
            "384m",
            "--pids-limit",
            "128",
            "--tmpfs",
            "/tmp:rw,size=32m",
            "--network",
            networks[0],
            "--log-opt",
            "max-size=5m",
            "--log-opt",
            "max-file=2",
            "--env-file",
            str(env_file),
            "--volume",
            f"{root/'backend'}:/app:ro",
            "--volume",
            f"{credentials}:/credentials.json:ro",
            "--volume",
            f"{args.output}:/research:rw",
            "--workdir",
            "/app",
        ]
        if args.watch:
            check = "import sqlite3,datetime; c=sqlite3.connect('file:/research/edge.sqlite3?mode=ro',uri=True); t=c.execute(\"SELECT MAX(available_at) FROM events WHERE kind='heartbeat'\").fetchone()[0]; assert t and (datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromisoformat(t)).total_seconds()<240"
            command += [
                "--detach",
                "--restart",
                "unless-stopped",
                "--health-cmd",
                f"python -c {shlex.quote(check)}",
                "--health-interval",
                "60s",
                "--health-start-period",
                "180s",
                "--health-retries",
                "3",
            ]
        else:
            command += ["--rm"]
        command += [
            metadata["Config"]["Image"],
            "-m",
            "edge_research.cli",
            "capture",
            "--archive",
            "/research/edge.sqlite3",
        ]
        if args.watch:
            command.append("--watch")
        subprocess.run(command, check=True)


if __name__ == "__main__":
    main()

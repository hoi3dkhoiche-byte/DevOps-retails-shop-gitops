#!/usr/bin/env python3
"""Render exact pinned platform charts and enforce each management node budget."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True, help="Chart cache in <name>/<name> layout")
    parser.add_argument("--download", action="store_true", help="Download missing exact chart pins over HTTPS")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    versions = yaml.safe_load((ROOT / "versions.yaml").read_text(encoding="utf-8"))
    if args.download:
        for name, pin in versions.items():
            if (args.cache / name / name / "Chart.yaml").is_file():
                continue
            destination = args.cache / name
            destination.mkdir(parents=True, exist_ok=True)
            subprocess.run(["helm", "pull", name, "--repo", pin["repository"], "--version", pin["version"],
                            "--untar", "--untardir", str(destination)], check=True, timeout=180)
    report = {"schema_version": 1, "profiles": validate.validate_platform_profiles(args.cache)}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for name, value in report["profiles"].items():
        print(f"{name}: {value['pods_on_management_including_hooks']} platform pods including hooks, "
              f"{value['cpu_requests_millicores']}m CPU requests, "
              f"{value['memory_requests_mib']}Mi memory requests / {value['memory_limits_mib']}Mi limits")
    print("Exact Helm renders and operator-created monitoring pods passed; no AWS or Kubernetes calls were made.")


if __name__ == "__main__":
    main()

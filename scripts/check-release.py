#!/usr/bin/env python3
"""Validate GitOps release inputs without registry, cluster or AWS writes."""
import os
from pathlib import Path
import subprocess
import sys

import yaml
from prepare import repository_credentials

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/release"))
from release import parse_json, release_from_values, require, validate_release


def main():
    values = {}
    for environment in ("dev", "prod"):
        path = ROOT / "environments" / environment / "images.yaml"
        values[environment] = release_from_values(parse_json(path.read_text(encoding="utf-8")), allow_disabled=True)
    require(values["dev"] is not None, "Dev must reference a verified five-image CI release")
    release = values["dev"]
    snapshot = ROOT / "releases" / (release["run"]["id"] + "-" + release["run"]["attempt"]) / "images.json"
    if snapshot.is_file():
        recorded = parse_json(snapshot.read_text(encoding="utf-8"))
        validate_release(recorded)
        require(recorded == release, "Dev differs from its recorded CI release")
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    repository_credentials(config)
    enabled = config.get("deployment", {}).get("enabled")
    require(type(enabled) is bool, "deployment.enabled must be a boolean")
    if enabled:
        subprocess.run([sys.executable, str(ROOT / "scripts/prepare.py"), "--check"], cwd=ROOT, check=True)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write("source_commit=" + release["source_commit"] + "\n")
            production = values["prod"] or release
            stream.write("prod_source_commit=" + production["source_commit"] + "\n")
    print("Verified five-image dev release " + release["source_commit"] + "; AWS deployment " + ("enabled" if enabled else "disabled"))
    print("Production release " + ("configured; synchronization remains manual" if values["prod"] else "disabled pending dev acceptance"))


if __name__ == "__main__":
    main()

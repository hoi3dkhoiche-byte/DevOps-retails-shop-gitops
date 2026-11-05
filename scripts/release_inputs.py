"""Verify the historical dev proof retained with a production release."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/release"))
from release import parse_json, require, validate_acceptance


def require_production_acceptance(root, release):
    if release is None:
        return
    path = Path(root) / "environments/prod/accepted.json"
    require(path.is_file(), "Enabled production release requires environments/prod/accepted.json")
    accepted = parse_json(path.read_text(encoding="utf-8"))
    # This is the DEV acceptance that authorized these PROD digests. It remains
    # valid when dev later advances; never compare it with current dev images.
    validate_acceptance(accepted, release, release["source_commit"])

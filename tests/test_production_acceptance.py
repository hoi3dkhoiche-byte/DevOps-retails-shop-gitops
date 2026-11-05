"""Production keeps its own exact dev proof even when dev moves to a newer release."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_inputs
from release import SERVICES, SOURCE_REPO


def release_and_proof():
    sha = "a" * 40
    run = {"id": "123", "attempt": "1", "url": f"https://github.com/{SOURCE_REPO}/actions/runs/123/attempts/1"}
    images = {}
    for name, repository in SERVICES.items():
        digest = "sha256:" + hashlib.sha256(name.encode()).hexdigest()
        images[name] = {"repository": repository, "digest": digest, "reference": repository + "@" + digest,
                        "tag": f"sha-{sha}-run-123-1", "image_id": "sha256:" + "b" * 64}
    release = {"schema_version": 1, "source_commit": sha, "run": run, "images": images,
               "platform": "linux/amd64", "backend_profile": "mysql-redis-opensearch"}
    proof = {"schema_version": 1, "environment": "dev", "source_commit": sha, "release_run": run,
             "images": {name: image["reference"] for name, image in images.items()},
             "checks": {"ready": True, "image_identity": True, "smoke": True},
             "checked_at": "2026-11-05T08:00:00+00:00", "operator": "operator",
             "pods": {name: [{"name": name + "-pod", "ready": True, "image_reference": image["reference"],
                              "runtime_image_id": image["reference"], "verified_config_digest": image["image_id"]}]
                      for name, image in images.items()}}
    return release, proof


class ProductionAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "environments/prod/accepted.json"
        self.path.parent.mkdir(parents=True)
        self.release, self.proof = release_and_proof()

    def write(self, proof):
        self.path.write_text(json.dumps(proof), encoding="utf-8")

    def test_disabled_production_needs_no_fabricated_proof(self):
        release_inputs.require_production_acceptance(self.root, None)

    def test_enabled_production_rejects_missing_proof(self):
        with self.assertRaisesRegex(ValueError, "accepted.json"):
            release_inputs.require_production_acceptance(self.root, self.release)

    def test_prod_uses_its_own_proof_after_current_dev_advances(self):
        self.write(self.proof)
        dev = self.root / "environments/dev"
        dev.mkdir()
        # Invalid/current dev data must not replace the exact historical proof.
        (dev / "accepted.json").write_text('{}', encoding='utf-8')
        (dev / "images.yaml").write_text('{"release":{"source_commit":"newer-dev"}}', encoding='utf-8')
        release_inputs.require_production_acceptance(self.root, self.release)

    def test_changed_digest_run_or_failed_smoke_cannot_be_promoted(self):
        changes = []
        value = copy.deepcopy(self.proof); value["images"]["ui"] += "x"; changes.append(value)
        value = copy.deepcopy(self.proof); value["release_run"]["attempt"] = "2"; changes.append(value)
        value = copy.deepcopy(self.proof); value["checks"]["smoke"] = False; changes.append(value)
        for value in changes:
            with self.subTest(proof=value), self.assertRaises(ValueError):
                self.write(value)
                release_inputs.require_production_acceptance(self.root, self.release)

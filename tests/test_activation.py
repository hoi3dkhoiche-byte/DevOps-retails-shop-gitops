"""Exercise activation through real Helm renders without contacting a cluster."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import validate


class ActivationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def config(self, value):
        path = self.directory / "target.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def values(self, chart, config):
        return [config, ROOT / "versions.yaml"] if chart == "bootstrap" else [config]

    def test_all_four_charts_are_empty_without_account_activation(self):
        disabled = self.config({"deployment": {"enabled": False}})
        for chart in validate.LOCAL_CHARTS:
            with self.subTest(chart=chart):
                self.assertEqual(validate.render(ROOT / chart), [])
                values = self.values(chart, ROOT / "config.yaml")
                if chart == "applications":
                    values.append(ROOT / "environments/dev/images.yaml")
                values.append(disabled)
                self.assertEqual(validate.render(ROOT / chart, values), [])

    def test_flipping_the_flag_cannot_activate_placeholder_account_values(self):
        config = validate.offline_target_config()
        config["accountId"] = "REPLACE_FROM_TERRAFORM"
        path = self.config(config)
        for chart in validate.LOCAL_CHARTS:
            with self.subTest(chart=chart):
                validate.render(ROOT / chart, self.values(chart, path), expect_failure=True)

    def test_activation_requires_all_target_identity_and_access_values(self):
        baseline = validate.offline_target_config()
        mutations = {
            "vpc": {"vpcId": "REPLACE_FROM_TERRAFORM"},
            "node-sg": {"nodeSecurityGroupId": "REPLACE_FROM_TERRAFORM"},
            "orders-sg": {"ordersSecurityGroupId": "REPLACE_FROM_TERRAFORM"},
            "role": {"roles": dict(baseline["roles"], **{"eso-dev": "arn:aws:iam::999999999999:role/wrong"})},
            "documentation-ip": {"adminCidrs": ["203.0.113.10/32"]},
            "private-ip": {"adminCidrs": ["10.1.2.3/32"]},
            "wide-cidr": {"adminCidrs": ["8.8.8.0/24"]},
            "bucket": {"buckets": dict(baseline["buckets"], logs="REPLACE_LOG_BUCKET")},
            "certificate": {"domain": "example.test", "certificateArn": ""},
        }
        for name, mutation in mutations.items():
            config = copy.deepcopy(baseline)
            config.update(mutation)
            path = self.config(config)
            for chart in validate.LOCAL_CHARTS:
                with self.subTest(mutation=name, chart=chart):
                    validate.render(ROOT / chart, self.values(chart, path), expect_failure=True)

    def test_boolean_flag_cannot_be_replaced_with_a_truthy_string(self):
        config = validate.offline_target_config()
        config["deployment"]["enabled"] = "false"
        path = self.config(config)
        for chart in validate.LOCAL_CHARTS:
            with self.subTest(chart=chart):
                validate.render(ROOT / chart, self.values(chart, path), expect_failure=True)

    def test_offline_valid_target_renders_each_chart_and_both_release_modes(self):
        path = self.config(validate.offline_target_config())
        for chart in validate.LOCAL_CHARTS:
            with self.subTest(chart=chart):
                self.assertTrue(validate.render(ROOT / chart, self.values(chart, path)))
        release = self.directory / "release.json"
        release.write_text(json.dumps({
            "release": {"enabled": True, "source_commit": "a" * 40,
                        "backend_profile": "mysql-redis-opensearch"},
            "images": {service: {"repository": "docker.io/hmannnguye/devops-retails-shop" +
                                  ("" if service == "ui" else "-" + service), "digest": "sha256:" + "b" * 64}
                       for service in validate.SERVICES},
        }), encoding="utf-8")
        for environment in ("dev", "prod"):
            docs = validate.render(ROOT / "applications", [path, release], {"environment": environment})
            apps = [document for document in docs if document["kind"] == "Application"]
            self.assertEqual(len(apps), 5)
            for app in apps:
                self.assertEqual(app["spec"]["sources"][0]["targetRevision"], "a" * 40)
                self.assertEqual("automated" in app["spec"]["syncPolicy"], environment == "dev")

    def test_disabled_release_has_no_ingress_for_an_absent_ui(self):
        path = self.config(validate.offline_target_config())
        for environment in ("dev", "prod"):
            for domain in ("", "example.test"):
                parameters = {"environment": environment, "domain": domain}
                if domain:
                    parameters["certificateArn"] = "arn:aws:acm:ap-southeast-1:111122223333:certificate/01234567-89ab-cdef-0123-456789abcdef"
                docs = validate.render(ROOT / "applications", [path], parameters)
                with self.subTest(environment=environment, domain=domain):
                    self.assertFalse(any(item["kind"] in {"Application", "Ingress"} for item in docs))


if __name__ == "__main__":
    unittest.main()

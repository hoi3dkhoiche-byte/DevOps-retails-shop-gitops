"""Reject bootstrap ordering that deadlocks Argo's child-Application health gate."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import validate


def resource(kind, name, wave, **extra):
    return {"kind": kind, "metadata": {"name": name, "annotations": {
        "argocd.argoproj.io/sync-wave": str(wave)}}, **extra}


class BootstrapOrderingTests(unittest.TestCase):
    def setUp(self):
        self.documents = [resource("Namespace", "retail-dev", -20),
                          resource("Namespace", "retail-prod", -20),
                          resource("AppProject", "retail-platform", -20),
                          resource("Application", "external-secrets", -5,
                                   spec={"project": "retail-platform"})]

    def test_prerequisites_precede_child_health_gate(self):
        validate.validate_bootstrap_order(self.documents)

    def test_missing_or_late_project_cannot_start_children(self):
        with self.assertRaisesRegex(ValueError, "AppProject"):
            validate.validate_bootstrap_order([item for item in self.documents if item["kind"] != "AppProject"])
        for wave in (-5, 0):
            documents = copy.deepcopy(self.documents)
            documents[2]["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] = str(wave)
            with self.subTest(wave=wave), self.assertRaisesRegex(ValueError, "AppProject"):
                validate.validate_bootstrap_order(documents)

    def test_missing_or_late_namespace_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "namespaces"):
            validate.validate_bootstrap_order(self.documents[1:])
        documents = copy.deepcopy(self.documents)
        documents[0]["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] = "0"
        with self.assertRaisesRegex(ValueError, "namespaces"):
            validate.validate_bootstrap_order(documents)


class EnvironmentOrderingTests(unittest.TestCase):
    def setUp(self):
        self.documents = [resource("SecurityGroupPolicy", "orders-backends", 0),
                          resource("ExternalSecret", "orders-runtime", 0),
                          resource("Application", "retail-dev-orders", 10),
                          resource("Ingress", "retail-ui", 20)]

    def test_backend_policies_precede_pods_and_ingress_follows_services(self):
        validate.validate_environment_order(self.documents)

    def test_late_secret_or_security_group_policy_is_rejected(self):
        for index in (0, 1):
            documents = copy.deepcopy(self.documents)
            documents[index]["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] = "10"
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "precede"):
                validate.validate_environment_order(documents)

    def test_missing_security_group_policy_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "SecurityGroupPolicy"):
            validate.validate_environment_order(self.documents[1:])

    def test_ingress_cannot_start_before_its_service(self):
        documents = copy.deepcopy(self.documents)
        documents[-1]["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] = "0"
        with self.assertRaisesRegex(ValueError, "Ingress"):
            validate.validate_environment_order(documents)


if __name__ == "__main__":
    unittest.main()

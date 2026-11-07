"""Render the real ALB entrypoints and reject bypasses or hostless route collisions."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import validate

PREFIX = "alb.ingress.kubernetes.io/"


class ApplicationIngressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        directory = Path(cls.temporary.name)
        cls.config = validate.offline_target_config()
        cls.config["adminCidrs"] = ["8.8.8.8/32", "1.1.1.1/32"]
        target = directory / "target.json"
        target.write_text(json.dumps(cls.config), encoding="utf-8")
        cls.release = directory / "release.json"
        cls.release.write_text(json.dumps({
            "release": {"enabled": True, "source_commit": "a" * 40,
                        "backend_profile": "mysql-redis-opensearch"},
            "images": {service: {"repository": "docker.io/hmannnguye/devops-retails-shop" +
                                  ("" if service == "ui" else "-" + service), "digest": "sha256:" + "b" * 64}
                       for service in validate.SERVICES},
        }), encoding="utf-8")
        cls.http = {}
        cls.https = {}
        cls.domain_config = dict(cls.config, domain="example.test",
            certificateArn="arn:aws:acm:ap-southeast-1:111122223333:certificate/01234567-89ab-cdef-0123-456789abcdef")
        for environment in ("dev", "prod"):
            cls.http[environment] = validate.render(ROOT / "applications", [target, cls.release],
                                                   {"environment": environment})
            cls.https[environment] = validate.render(ROOT / "applications", [target, cls.release], {
                "environment": environment, "domain": cls.domain_config["domain"],
                "certificateArn": cls.domain_config["certificateArn"]})
        cls.target = target

    def test_domainless_dev_and_prod_both_expose_ip_restricted_http_without_group_collision(self):
        groups = set()
        for environment, documents in self.http.items():
            with self.subTest(environment=environment):
                ingress = validate.validate_ingress(documents, environment, self.config)
                annotations = ingress["metadata"]["annotations"]
                groups.add(annotations[PREFIX + "group.name"])
                self.assertEqual(annotations[PREFIX + "inbound-cidrs"], "8.8.8.8/32,1.1.1.1/32")
                self.assertEqual(ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"],
                                 "ui-active")
        self.assertEqual(groups, {"retail-dev", "retail-prod"})

    def test_domain_routes_share_https_alb_with_separate_hosts_and_dev_source_condition(self):
        hosts = set()
        for environment, documents in self.https.items():
            with self.subTest(environment=environment):
                ingress = validate.validate_ingress(documents, environment, self.domain_config)
                annotations = ingress["metadata"]["annotations"]
                self.assertEqual(annotations[PREFIX + "group.name"], "retail-public")
                self.assertEqual(annotations[PREFIX + "ssl-redirect"], "443")
                self.assertEqual(annotations[PREFIX + "certificate-arn"], self.domain_config["certificateArn"])
                self.assertEqual(PREFIX + "conditions.ui-active" in annotations, environment == "dev")
                hosts.add(ingress["spec"]["rules"][0]["host"])
        self.assertEqual(hosts, {"dev.retail.example.test", "retail.example.test"})

    def test_disabled_target_never_exposes_an_enabled_release(self):
        disabled = Path(self.temporary.name) / "disabled.json"
        disabled.write_text('{"deployment":{"enabled":false}}', encoding="utf-8")
        for environment in ("dev", "prod"):
            with self.subTest(environment=environment):
                documents = validate.render(ROOT / "applications", [self.target, self.release, disabled],
                                            {"environment": environment})
                self.assertEqual(documents, [])

    def test_http_rejects_shared_hostless_group_or_broadened_admin_access(self):
        for key, value in (("group.name", "retail-public"), ("inbound-cidrs", "0.0.0.0/0")):
            documents = copy.deepcopy(self.http["dev"])
            ingress = next(document for document in documents if document["kind"] == "Ingress")
            ingress["metadata"]["annotations"][PREFIX + key] = value
            with self.subTest(annotation=key), self.assertRaises(ValueError):
                validate.validate_ingress(documents, "dev", self.config)

    def test_https_rejects_disabled_redirect_or_missing_dev_ip_filter(self):
        for key in ("ssl-redirect", "conditions.ui-active"):
            documents = copy.deepcopy(self.https["dev"])
            ingress = next(document for document in documents if document["kind"] == "Ingress")
            del ingress["metadata"]["annotations"][PREFIX + key]
            with self.subTest(annotation=key), self.assertRaises(ValueError):
                validate.validate_ingress(documents, "dev", self.domain_config)

    def test_alb_must_target_active_ui_pods(self):
        for mutation in ("preview", "instance"):
            documents = copy.deepcopy(self.http["dev"])
            ingress = next(document for document in documents if document["kind"] == "Ingress")
            if mutation == "preview":
                ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"] = "ui-preview"
            else:
                ingress["metadata"]["annotations"][PREFIX + "target-type"] = "instance"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate.validate_ingress(documents, "dev", self.config)


class LoadBalancerSecurityGroupTests(unittest.TestCase):
    def controller(self, args):
        return {"kind": "Deployment", "metadata": {"labels": {
            "app.kubernetes.io/name": "aws-load-balancer-controller"}}, "spec": {"template": {"spec": {
                "containers": [{"name": "controller", "args": args}]}}}}

    def test_controller_requires_backend_security_group_management(self):
        validate.validate_load_balancer_controller([self.controller(["--enable-backend-security-group=true"])])
        for args in ([], ["--enable-backend-security-group=false"]):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, "backend security group"):
                validate.validate_load_balancer_controller([self.controller(args)])


if __name__ == "__main__":
    unittest.main()

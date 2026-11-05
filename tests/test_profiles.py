"""Profiles must preserve tested releases and fit the selected management node."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import validate
import prepare
from platform_budget import rendered_pods, validate_budget
from profiles import deployment_profile, platform_charts, values_files


def container(name="controller", cpu="100m", memory="64Mi", limit="128Mi"):
    return {"name": name, "resources": {"requests": {"cpu": cpu, "memory": memory},
                                         "limits": {"cpu": "2000m", "memory": limit}}}


def workload(containers):
    return {"kind": "Deployment", "metadata": {"name": "budget-fixture"}, "spec": {
        "replicas": 1, "template": {"spec": {"nodeSelector": {"workload": "management"}, "containers": containers}}}}


class ProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_legacy_profile_retains_full_platform_and_lab_omits_optional_children(self):
        self.assertEqual(deployment_profile({}), "architecture")
        self.assertEqual(len(platform_charts({})), 9)
        self.assertEqual(set(platform_charts({"deploymentProfile": "lab"})),
                         {"argo-rollouts", "aws-load-balancer-controller", "external-secrets", "metrics-server", "kube-prometheus-stack"})

    def test_both_profile_graphs_render_with_their_exact_overlays(self):
        for profile in ("lab", "architecture"):
            with self.subTest(profile=profile):
                validate.validate_profile_graph(profile, self.directory)

    def test_unknown_or_boolean_profiles_cannot_change_the_resource_graph(self):
        for value in ("small", False, None, 1, {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "deploymentProfile"):
                deployment_profile({"deploymentProfile": value})
        config = validate.offline_target_config()
        config["deploymentProfile"] = "small"
        path = self.directory / "bad.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        for chart in validate.LOCAL_CHARTS:
            with self.subTest(chart=chart):
                values = [path] + ([ROOT / "versions.yaml"] if chart == "bootstrap" else [])
                validate.render(ROOT / chart, values, expect_failure=True)

    def test_lab_refuses_enabled_production_workloads(self):
        config = validate.offline_target_config("lab")
        config.update(environment="prod", release={"enabled": True})
        path = self.directory / "prod-lab.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        error = validate.render(ROOT / "applications", [path], expect_failure=True)
        self.assertIn("Lab profile forbids production", error)

    def test_lab_overlay_is_required_and_does_not_mutate_the_shared_values(self):
        with self.assertRaisesRegex(ValueError, "Missing lab"):
            values_files(self.directory, "argo-cd", {"deploymentProfile": "lab"})
        base = (ROOT / "platform/argo-cd.yaml").read_bytes()
        files = values_files(ROOT, "argo-cd", {"deploymentProfile": "lab"})
        self.assertEqual([value.relative_to(ROOT).as_posix() for value in files],
                         ["platform/argo-cd.yaml", "profiles/lab/argo-cd.yaml"])
        self.assertEqual((ROOT / "platform/argo-cd.yaml").read_bytes(), base)

    def test_prepare_imports_optional_profile_without_changing_a_release(self):
        shutil.copytree(ROOT / "platform", self.directory / "platform")
        shutil.copytree(ROOT / "applications/files", self.directory / "applications/files")
        shutil.copy2(ROOT / "config.yaml", self.directory / "config.yaml")
        config = validate.offline_target_config()
        outputs = {source: config[target] for target, source in {
            "accountId": "account_id", "vpcId": "vpc_id", "nodeSecurityGroupId": "node_security_group_id",
            "ordersSecurityGroupId": "orders_security_group_id", "adminCidrs": "admin_cidrs", "roles": "workload_roles",
            "buckets": "buckets", "certificateArn": "certificate_arn"}.items()}
        before = (ROOT / "environments/dev/images.yaml").read_bytes()
        with patch.object(prepare, "ROOT", self.directory):
            self.assertEqual(prepare.prepare(outputs)["deploymentProfile"], "architecture")
            live = yaml.safe_load((self.directory / "config.yaml").read_text(encoding="utf-8"))
            live["deployment"]["enabled"] = True
            (self.directory / "config.yaml").write_text(yaml.safe_dump(live), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "teardown first"):
                prepare.prepare(dict(outputs, deployment_profile="lab"))
            live["deployment"]["enabled"] = False
            (self.directory / "config.yaml").write_text(yaml.safe_dump(live), encoding="utf-8")
            for profile in ("lab", "architecture"):
                native = {key: {"value": value} for key, value in dict(outputs, deployment_profile=profile).items()}
                result = prepare.prepare(native)
                self.assertEqual(result["deploymentProfile"], profile)
                self.assertFalse(result["deployment"]["enabled"])
            outputs["deployment_profile"] = "small"
            with self.assertRaisesRegex(ValueError, "deploymentProfile"):
                prepare.prepare(outputs)
        self.assertEqual((ROOT / "environments/dev/images.yaml").read_bytes(), before)

    def test_lab_external_alert_delivery_requires_the_full_profile(self):
        config = validate.offline_target_config("lab")
        config["alerting"] = {"enabled": True}
        with self.assertRaisesRegex(ValueError, "Alertmanager"):
            prepare.validate(config)


class CapacityTests(unittest.TestCase):
    def test_unbudgeted_containers_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Explicit CPU/memory"):
            validate_budget([workload([{"name": "unbounded"}])], "lab")

    def test_native_sidecars_are_counted_during_later_init_containers(self):
        document = workload([container(cpu="50m")])
        native = container("sidecar", cpu="25m")
        native["restartPolicy"] = "Always"
        document["spec"]["template"]["spec"]["initContainers"] = [native, container("init", cpu="500m", memory="128Mi")]
        pod = rendered_pods([document])[0]
        self.assertEqual(pod["requests_cpu"], 525)
        self.assertEqual(pod["requests_memory"], 192 * 1024**2)

    def test_cpu_and_memory_capacity_checks_include_a_rolling_replacement(self):
        with self.assertRaisesRegex(ValueError, "CPU requests"):
            validate_budget([workload([container(cpu="800m")])], "lab")
        with self.assertRaisesRegex(ValueError, "memory limits"):
            validate_budget([workload([container(limit="4Gi")])], "lab")

    def test_prometheus_operator_created_pods_are_counted(self):
        operator = workload([container("operator")])
        operator["spec"]["template"]["spec"]["containers"][0]["args"] = [
            "--config-reloader-cpu-request=10m", "--config-reloader-cpu-limit=100m",
            "--config-reloader-memory-request=32Mi", "--config-reloader-memory-limit=64Mi"]
        prometheus = {"kind": "Prometheus", "metadata": {"name": "metrics"}, "spec": {
            "nodeSelector": {"workload": "management"}, "replicas": 1, "resources": container()["resources"]}}
        pods = rendered_pods([operator, prometheus])
        generated = next(value for value in pods if value["workload"].startswith("Prometheus/"))
        self.assertEqual(generated["requests_cpu"], 110)
        self.assertEqual(generated["limits_memory"], 192 * 1024**2)
        prometheus["spec"].pop("resources")
        with self.assertRaisesRegex(ValueError, "Explicit CPU/memory"):
            rendered_pods([operator, prometheus])


if __name__ == "__main__":
    unittest.main()

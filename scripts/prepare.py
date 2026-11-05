#!/usr/bin/env python3
"""Prepare non-secret AWS values; activate only after complete validation."""
import argparse
import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import yaml
from profiles import deployment_profile

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("lb", "autoscaler", "rollouts", "eso-dev", "eso-prod", "eso-monitoring", "eso-argocd", "grafana", "loki", "velero")
ROLE_ANNOTATION = "eks.amazonaws.com/role-arn"
REPOSITORY_PREFIX = "https://github.com/hoi3dkhoiche-byte/"


class ReadableDumper(yaml.SafeDumper):
    def ignore_aliases(self, value):
        return True


def multiline_string(dumper, value):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


ReadableDumper.add_representer(str, multiline_string)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    value = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    require(isinstance(value, dict), "Expected a YAML mapping: " + str(path.relative_to(ROOT)))
    return value


def serialize(value):
    return yaml.dump(value, Dumper=ReadableDumper, sort_keys=False)


def write_atomic(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                         prefix=path.name + ".tmp-", delete=False) as output:
            temporary = Path(output.name)
            output.write(serialize(value))
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate(config, enabled=False):
    profile = deployment_profile(config)
    require(profile != "lab" or not config.get("alerting", {}).get("enabled"),
            "Lab omits Alertmanager; use architecture for external alert delivery")
    require(isinstance(config.get("deployment", {}), dict), "deployment must be a YAML mapping")
    if enabled:
        require(config.get("deployment", {}).get("enabled") is True,
                "Deployment is disabled. Review Terraform outputs and prepare with --activate before bootstrap.")
    require(config.get("region") == "ap-southeast-1" and config.get("clusterName") == "retail-lab-eks",
            "Unexpected target region/cluster")
    account = str(config.get("accountId", ""))
    require(re.fullmatch(r"[0-9]{12}", account) is not None and account != "123456789012",
            "Supply the actual AWS account ID")
    for name, prefix in (("vpcId", "vpc-"), ("nodeSecurityGroupId", "sg-"), ("ordersSecurityGroupId", "sg-")):
        require(re.fullmatch(prefix + r"[0-9a-f]{8,17}", str(config.get(name, ""))) is not None, "Invalid " + name)
    require(isinstance(config.get("adminCidrs"), list) and bool(config["adminCidrs"]),
            "At least one administration /32 is required")
    examples = [ipaddress.ip_network(value) for value in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
    for cidr in config["adminCidrs"]:
        network = ipaddress.ip_network(cidr)
        require(network.version == 4 and network.prefixlen == 32 and network.network_address.is_global
                and not any(network.subnet_of(example) for example in examples),
                "Use your actual public administration IPv4 /32")
    require(isinstance(config.get("roles"), dict), "Supply workload IAM roles from Terraform outputs")
    for name in ROLES:
        require(re.fullmatch(r"arn:aws:iam::" + account + r":role/.+", config["roles"].get(name, "")) is not None,
                "Missing or wrong-account IAM role: " + name)
    require(isinstance(config.get("buckets"), dict), "Supply the logs and backup buckets from Terraform outputs")
    for name in ("logs", "backup"):
        require(config["buckets"].get(name) == "retail-lab-" + account + "-" + name, "Unexpected " + name + " bucket")
    if config.get("domain"):
        require(re.fullmatch(r"arn:aws:acm:ap-southeast-1:" + account + r":certificate/[0-9a-f-]+",
                             config.get("certificateArn", "")) is not None,
                "HTTPS requires a validated ACM certificate in this account/region")
    require("REPLACE_" not in serialize(config), "Unfilled values remain in config.yaml")
    repository_credentials(config)


def repository_credentials(config):
    credentials = config.get("repositoryCredentials", {"enabled": False})
    require(isinstance(credentials, dict), "repositoryCredentials must be a YAML mapping")
    require(set(credentials) <= {"enabled", "urlPrefix"},
            "repositoryCredentials accepts metadata only; store username/password in Secrets Manager")
    require(type(credentials.get("enabled")) is bool, "repositoryCredentials.enabled must be a boolean")
    if credentials["enabled"]:
        require(credentials.get("urlPrefix") == REPOSITORY_PREFIX,
                "Repository credential URL prefix must be " + REPOSITORY_PREFIX + " including the trailing slash")
        for name in ("sourceRepo", "gitopsRepo"):
            require(re.fullmatch(re.escape(REPOSITORY_PREFIX) + r"[A-Za-z0-9][A-Za-z0-9_.-]*\.git",
                                 str(config.get(name, ""))) is not None,
                    "Repository credentials must cover the HTTPS .git URL in " + name)
    return credentials


def trust_material():
    for name in ("rds-ca.pem", "rds-truststore.p12"):
        path = ROOT / "applications/files" / name
        require(path.is_file() and path.stat().st_size > 0, "Missing public RDS trust material: " + name)


def platform_values(config):
    documents = {path: read(path) for path in sorted((ROOT / "platform").glob("*.yaml"))}
    for name, role in (("aws-load-balancer-controller", "lb"), ("argo-rollouts", "rollouts"), ("loki", "loki")):
        content = documents[ROOT / "platform" / (name + ".yaml")]
        content["serviceAccount"].setdefault("annotations", {})[ROLE_ANNOTATION] = config["roles"][role]
        if name == "aws-load-balancer-controller":
            content["vpcId"] = config["vpcId"]
        if name == "loki":
            content["loki"]["storage"]["bucketNames"] = dict.fromkeys(("chunks", "ruler", "admin"), config["buckets"]["logs"])
    content = documents[ROOT / "platform/cluster-autoscaler.yaml"]
    content["rbac"]["serviceAccount"].setdefault("annotations", {})[ROLE_ANNOTATION] = config["roles"]["autoscaler"]
    content = documents[ROOT / "platform/kube-prometheus-stack.yaml"]
    content["grafana"]["serviceAccount"].setdefault("annotations", {})[ROLE_ANNOTATION] = config["roles"]["grafana"]
    if config.get("alerting", {}).get("enabled"):
        content["alertmanager"]["alertmanagerSpec"]["configSecret"] = "alertmanager-notification-config"
    else:
        content["alertmanager"]["alertmanagerSpec"].pop("configSecret", None)
    content = documents[ROOT / "platform/velero.yaml"]
    content["serviceAccount"]["server"].setdefault("annotations", {})[ROLE_ANNOTATION] = config["roles"]["velero"]
    content["configuration"]["backupStorageLocation"][0]["bucket"] = config["buckets"]["backup"]
    return documents


def check_platforms(config, documents):
    for path, value in documents.items():
        require("REPLACE_" not in serialize(value), "Unfilled platform value in " + str(path.relative_to(ROOT)))
    expected = platform_values(config)
    for path, value in expected.items():
        require(documents[path] == value, "Platform configuration does not match config.yaml: " + path.name)
    load_balancer = documents[ROOT / "platform/aws-load-balancer-controller.yaml"]
    require(load_balancer["clusterName"] == config["clusterName"] and load_balancer["region"] == config["region"],
            "Load balancer controller targets a different cluster/region")
    autoscaler = documents[ROOT / "platform/cluster-autoscaler.yaml"]
    require(autoscaler["autoDiscovery"]["clusterName"] == config["clusterName"]
            and autoscaler["awsRegion"] == config["region"], "Autoscaler targets a different cluster/region")


def prepare(outputs=None, domain=None, alerting=None, activate=False):
    original = read(ROOT / "config.yaml")
    config = copy.deepcopy(original)
    if outputs is not None:
        values = {key: value["value"] if isinstance(value, dict) and "value" in value else value
                  for key, value in outputs.items()}
        mapping = {"accountId": "account_id", "vpcId": "vpc_id", "nodeSecurityGroupId": "node_security_group_id",
                   "ordersSecurityGroupId": "orders_security_group_id", "adminCidrs": "admin_cidrs", "roles": "workload_roles",
                   "buckets": "buckets", "certificateArn": "certificate_arn"}
        for target, source in mapping.items():
            require(source in values, "Missing Terraform output: " + source)
            config[target] = values[source]
        config["domain"] = values.get("domain", "")
        config["deploymentProfile"] = values.get("deployment_profile", "architecture")
    if domain is not None:
        config["domain"] = domain
    if alerting is not None:
        config.setdefault("alerting", {})["enabled"] = alerting
    require(original.get("deployment", {}).get("enabled") is not True
            or deployment_profile(original) == deployment_profile(config),
            "Changing a live deployment profile requires teardown first; optional Applications are not automatically pruned")
    config.setdefault("deployment", {})["enabled"] = bool(activate)
    validate(config)
    trust_material()
    documents = platform_values(config)
    check_platforms(config, documents)
    # Serialize all candidate documents before changing the worktree. Keep the
    # deployment disabled during replacement; the enabled config is written last.
    for value in documents.values():
        serialize(value)
    serialize(config)
    if original.get("deployment", {}).get("enabled") is True:
        disabled = copy.deepcopy(original)
        disabled["deployment"]["enabled"] = False
        write_atomic(ROOT / "config.yaml", disabled)
    for path, value in documents.items():
        write_atomic(path, value)
    write_atomic(ROOT / "config.yaml", config)
    return config


def check():
    config = read(ROOT / "config.yaml")
    validate(config, enabled=True)
    trust_material()
    documents = {path: read(path) for path in sorted((ROOT / "platform").glob("*.yaml"))}
    check_platforms(config, documents)
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terraform-output", type=Path, help="JSON generated by terraform output -json; never a state file")
    parser.add_argument("--domain")
    parser.add_argument("--enable-alerting", action="store_true", default=None)
    parser.add_argument("--activate", action="store_true", help="Enable deployment only after all non-secret target values validate")
    parser.add_argument("--check", action="store_true", help="Read-only: require deployment enabled and complete target values")
    args = parser.parse_args()
    require(args.terraform_output or args.activate or args.check, "Use --terraform-output FILE, --activate or --check")
    if args.terraform_output or args.activate:
        outputs = json.loads(args.terraform_output.read_text(encoding="utf-8-sig")) if args.terraform_output else None
        config = prepare(outputs, args.domain, args.enable_alerting, args.activate)
        print("GitOps values prepared; deployment " + ("enabled." if config["deployment"]["enabled"] else "remains disabled."))
    if args.check:
        check()
        print("Deployment is enabled and non-secret target configuration is complete.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, TypeError) as error:
        sys.exit(str(error))

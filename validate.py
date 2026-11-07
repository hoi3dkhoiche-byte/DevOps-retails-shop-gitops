#!/usr/bin/env python3
"""Render the GitOps graph and check routing, isolation and rollout invariants offline."""
import argparse
import copy
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import yaml

GITOPS = Path(__file__).resolve().parent
sys.path.insert(0, str(GITOPS / "scripts"))
from prepare import repository_credentials
from profiles import deployment_profile, platform_charts, values_files
from platform_budget import validate_budget
from release_inputs import require_production_acceptance
from release import release_from_values
SOURCE = next((candidate for candidate in (GITOPS.parent / "retail-store-sample-app",
                                          Path.cwd() / "retail-store-sample-app")
               if (candidate / "src/ui/chart").is_dir()), GITOPS.parent / "retail-store-sample-app")
SERVICES = ("ui", "catalog", "cart", "checkout", "orders")
LOCAL_CHARTS = ("bootstrap", "applications", "argocd-secrets", "platform-resources")
ROLE_NAMES = ("lb", "autoscaler", "rollouts", "eso-dev", "eso-prod", "eso-monitoring", "eso-argocd", "grafana", "loki", "velero")


class RenderedLoader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    """Safe loader accepting the bare '=' enum emitted by upstream CRD generators."""


RenderedLoader.add_constructor("tag:yaml.org,2002:value", RenderedLoader.construct_scalar)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sync_wave(document):
    return int(document["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-wave", "0"))


def render(chart, values=(), parameters=None, namespace="default", release="retail-check", expect_failure=False,
           include_crds=False):
    command = ["helm", "template", release, str(chart), "--namespace", namespace, "--kube-version", "1.36.0", "--skip-tests"]
    if include_crds:
        command.append("--include-crds")
    for value in values:
        command.extend(["-f", str(value)])
    for name, value in (parameters or {}).items():
        command.extend(["--set-string", name + "=" + value])
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=90)
    if expect_failure:
        require(result.returncode != 0, "Unprepared activation unexpectedly rendered: " + str(chart))
        require("Activation requires" in result.stderr or "Administration" in result.stderr
                or "Placeholder/private" in result.stderr or "deployment.enabled" in result.stderr
                or "controlled domain" in result.stderr or "deploymentProfile" in result.stderr
                or "Lab profile" in result.stderr,
                "Activation failed for an unrelated Helm error: " + result.stderr)
        return result.stderr
    require(result.returncode == 0, result.stderr)
    return [document for document in yaml.load_all(result.stdout, Loader=RenderedLoader) if document]


def offline_target_config(profile="architecture"):
    """Explicit test fixture only; never write these values to config.yaml."""
    config = copy.deepcopy(yaml.safe_load((GITOPS / "config.yaml").read_text(encoding="utf-8")))
    account = "111122223333"
    config.update(deployment={"enabled": True}, deploymentProfile=profile, accountId=account, vpcId="vpc-11111111111111111",
                  nodeSecurityGroupId="sg-22222222222222222", ordersSecurityGroupId="sg-33333333333333333",
                  adminCidrs=["8.8.8.8/32"], domain="", certificateArn="",
                  roles={name: f"arn:aws:iam::{account}:role/offline-fixture-{name}" for name in ROLE_NAMES},
                  buckets={name: f"retail-lab-{account}-{name}" for name in ("logs", "backup")})
    return config


def validate_release(path):
    release = yaml.safe_load(path.read_text(encoding="utf-8"))
    require(isinstance(release, dict) and isinstance(release.get("release", {}).get("enabled"), bool),
            "Release enablement must be a boolean: " + str(path))
    if release["release"]["enabled"]:
        require(re.fullmatch(r"[0-9a-f]{40}", release["release"].get("source_commit", "")), "Release source commit must be immutable")
        require(release["release"].get("backend_profile") == "mysql-redis-opensearch", "Unaccepted release backend profile")
        require(set(release.get("images", {})) == set(SERVICES), "Release must contain exactly five services")
        for service in SERVICES:
            image = release["images"][service]
            repository = "docker.io/hmannnguye/devops-retails-shop" + ("" if service == "ui" else "-" + service)
            require(image.get("repository") == repository, "Unexpected image repository: " + service)
            require(re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("digest", "")), "Missing registry digest: " + service)
    return release


def validate_service_applications(documents, environment, release):
    applications = [document for document in documents if document["kind"] == "Application"]
    require(len(applications) == 5, "A release must declare exactly five service Applications")
    expected_names = {"retail-" + environment + "-" + service for service in SERVICES}
    require({document["metadata"]["name"] for document in applications} == expected_names,
            "Service Application names do not match the environment")
    for document in applications:
        service = document["metadata"]["name"].removeprefix("retail-" + environment + "-")
        source = document["spec"]["sources"][0]
        require(source["targetRevision"] == release["release"]["source_commit"],
                "Chart source must match the tested source commit")
        parameters = {parameter["name"]: parameter["value"] for parameter in source["helm"]["parameters"]}
        require(parameters.get("image.repository") == release["images"][service]["repository"]
                and parameters.get("image.digest") == release["images"][service]["digest"],
                "Rendered service does not use its verified CI repository/digest: " + service)
        require(document["spec"]["destination"]["namespace"] == "retail-" + environment,
                "Service escaped its environment namespace")
        require(("automated" in document["spec"]["syncPolicy"]) == (environment == "dev"),
                "Production application sync must be manual")
    validate_environment_order(documents)


def validate_environment_order(documents):
    applications = [document for document in documents if document["kind"] == "Application"]
    require(applications, "Enabled environment has no service Applications")
    prerequisites = [document for document in documents if document["kind"] in
                     {"ConfigMap", "SecretStore", "ExternalSecret", "SecurityGroupPolicy", "NetworkPolicy"}]
    require(any(document["kind"] == "SecurityGroupPolicy" for document in prerequisites),
            "Orders requires its SecurityGroupPolicy before pod creation")
    first_application = min(sync_wave(document) for document in applications)
    last_application = max(sync_wave(document) for document in applications)
    require(all(sync_wave(document) < first_application for document in prerequisites),
            "Environment secrets and policies must precede service Applications")
    require(all(sync_wave(document) > last_application for document in documents if document["kind"] == "Ingress"),
            "Ingress must follow service Applications")


def validate_ingress(documents, environment, config):
    """Check both domainless HTTP and shared-domain HTTPS application entrypoints."""
    ingresses = [document for document in documents if document["kind"] == "Ingress"]
    require(len(ingresses) == 1, "Each enabled environment requires exactly one ALB Ingress")
    ingress = ingresses[0]
    annotations = ingress["metadata"]["annotations"]
    prefix = "alb.ingress.kubernetes.io/"
    require(ingress["spec"].get("ingressClassName") == "alb"
            and annotations.get(prefix + "scheme") == "internet-facing"
            and annotations.get(prefix + "target-type") == "ip",
            "Application ingress must use the public ALB controller with Pod IP targets")
    rules = ingress["spec"]["rules"]
    require(len(rules) == 1, "Each environment requires one UI routing rule")
    paths = rules[0]["http"]["paths"]
    require(len(paths) == 1 and paths[0]["path"] == "/" and paths[0]["pathType"] == "Prefix"
            and paths[0]["backend"]["service"] == {"name": "ui-active", "port": {"number": 8080}},
            "ALB must route to the active UI on port 8080")
    require(annotations.get(prefix + "healthcheck-path") == "/actuator/health/readiness",
            "ALB requires the UI readiness health check")
    listeners = json.loads(annotations.get(prefix + "listen-ports", "[]"))
    if config.get("domain"):
        require(annotations.get(prefix + "group.name") == "retail-public",
                "Domain-based environments must share their public ALB")
        expected_host = ("dev." if environment == "dev" else "") + "retail." + config["domain"]
        require(rules[0].get("host") == expected_host, "Shared ALB requires distinct environment hostnames")
        require(listeners == [{"HTTP": 80}, {"HTTPS": 443}]
                and annotations.get(prefix + "ssl-redirect") == "443"
                and annotations.get(prefix + "certificate-arn") == config["certificateArn"],
                "Domain ingress requires its ACM certificate and HTTP-to-HTTPS redirect")
        if environment == "dev":
            condition = json.loads(annotations.get(prefix + "conditions.ui-active", "[]"))
            require(condition == [{"field": "source-ip", "sourceIpConfig": {"values": config["adminCidrs"]}}],
                    "Shared-domain dev ALB route must restrict client source IPs")
        else:
            require(prefix + "conditions.ui-active" not in annotations,
                    "Production must not inherit the dev-only source IP condition")
    else:
        require(annotations.get(prefix + "group.name") == "retail-" + environment,
                "Hostless dev/prod routes must use separate environment ALB groups")
        require("host" not in rules[0] and listeners == [{"HTTP": 80}],
                "Domainless ALB ingress must provide hostless HTTP access")
        require(annotations.get(prefix + "inbound-cidrs") == ",".join(config["adminCidrs"]),
                "Domainless ALB must allow only configured administration CIDRs")
        require(prefix + "certificate-arn" not in annotations and prefix + "ssl-redirect" not in annotations,
                "Domainless HTTP must not require an absent certificate")
    validate_environment_order(documents)
    return ingress


def validate_load_balancer_controller(documents):
    controllers = [document for document in documents if document["kind"] == "Deployment"
                   and document["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "aws-load-balancer-controller"]
    require(len(controllers) == 1, "Expected one AWS Load Balancer Controller deployment")
    containers = controllers[0]["spec"]["template"]["spec"]["containers"]
    require(any("--enable-backend-security-group=true" in container.get("args", []) for container in containers),
            "ALB controller must manage backend security group rules")


def validate_activation(config, temporary):
    actual = yaml.safe_load(config.read_text(encoding="utf-8"))
    repository_credentials(actual)
    enabled = actual.get("deployment", {}).get("enabled")
    require(isinstance(enabled, bool), "config.yaml deployment.enabled must be a boolean")
    if enabled:
        check = subprocess.run([sys.executable, str(GITOPS / "scripts/prepare.py"), "--check"],
                               capture_output=True, text=True, check=False, timeout=90)
        require(check.returncode == 0, "Prepared target check failed: " + check.stdout + check.stderr)
    disabled = temporary / "disabled.json"
    disabled.write_text(json.dumps({"deployment": {"enabled": False}}), encoding="utf-8")
    unprepared = temporary / "unprepared.json"
    unprepared.write_text(json.dumps({"deployment": {"enabled": True}, "accountId": "REPLACE_FROM_TERRAFORM"}), encoding="utf-8")
    for chart in LOCAL_CHARTS:
        values = [config, disabled]
        if chart == "bootstrap":
            values.insert(1, GITOPS / "versions.yaml")
        require(not render(GITOPS / chart, values), "Disabled chart emitted resources: " + chart)
        render(GITOPS / chart, [config, unprepared], expect_failure=True)
        if not enabled:
            current_values = [config] + ([GITOPS / "versions.yaml"] if chart == "bootstrap" else [])
            require(not render(GITOPS / chart, current_values), "Inactive repository emitted resources: " + chart)
    return actual


def validate_repository_secrets(documents, config):
    credentials = repository_credentials(config)
    secrets = [document for document in documents if document["kind"] == "ExternalSecret"]
    expected = {"argocd-secret", "argocd-redis"}
    if credentials["enabled"]:
        expected.add("argocd-github-credentials")
    require({document["spec"]["target"]["name"] for document in secrets} == expected,
            "Argo CD credentials must be supplied by the configured ESO ExternalSecrets")
    repositories = [document for document in secrets if document["metadata"]["name"] == "argocd-repositories"]
    require(len(repositories) == int(credentials["enabled"]), "Repository credential enablement mismatch")
    if repositories:
        spec = repositories[0]["spec"]
        template = spec["target"]["template"]
        require(template["metadata"]["labels"].get("argocd.argoproj.io/secret-type") == "repo-creds",
                "Argo CD needs a repo-creds Secret label")
        require(template["data"] == {"type": "git", "url": credentials["urlPrefix"],
                                    "username": "{{ .username }}", "password": "{{ .password }}"},
                "Repository credential Secret must reference ESO data and the shared URL prefix")
        require({(item["secretKey"], item["remoteRef"]["key"], item["remoteRef"]["property"])
                 for item in spec["data"]} == {
                    (key, "/retail/platform/argocd/repositories", key) for key in ("username", "password")},
                "Repository credentials must come from the dedicated Secrets Manager path")


def validate_source_checkout(source, release):
    require((source / "src/ui/chart").is_dir(), "Supply the application chart checkout: " + str(source))
    if release["release"]["enabled"]:
        result = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True,
                                text=True, check=False, timeout=30)
        require(result.returncode == 0 and result.stdout.strip() == release["release"]["source_commit"],
                "Chart checkout must match the release source_commit; use --source/--prod-source with the pinned checkouts")


def validate_source_chart(service, environment, source=None, image=None):
    values = GITOPS / "environments" / environment / (service + ".yaml")
    source = source or SOURCE
    image = image or {"repository": "docker.io/hmannnguye/devops-retails-shop" + ("" if service == "ui" else "-" + service), "digest": "sha256:" + "a" * 64}
    docs = render(source / "src" / service / "chart", [values], {"image.repository": image["repository"], "image.digest": image["digest"]})
    workload = next(document for document in docs if document["kind"] in ("Deployment", "Rollout"))
    pod = workload["spec"]["template"]["spec"]
    container = pod["containers"][0]
    require(container["image"] == image["repository"] + "@" + image["digest"], "Application chart lost the verified registry digest")
    ports = {port["name"]: port["containerPort"] for port in container["ports"]}
    for document in docs:
        if document["kind"] == "Service":
            for port in document["spec"]["ports"]:
                require(port["port"] == ports[port["targetPort"]] == 8080, "Native CNI network policies require service and container port 8080")
    selectors = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    require(any({"key": "workload", "operator": "In", "values": [environment]} in term["matchExpressions"] for term in selectors), "Workload must require its own node group")
    if service == "ui":
        require(workload["kind"] == "Rollout", "UI must use Argo Rollouts")
        strategy = workload["spec"]["strategy"]["blueGreen"]
        require(strategy["activeService"] == "ui-active" and strategy["previewService"] == "ui-preview", "Blue/Green service routing mismatch")
        require(strategy["autoPromotionEnabled"] == (environment == "dev"), "Production UI promotion must stay manual")
        hpa = next(document for document in docs if document["kind"] == "HorizontalPodAutoscaler")
        require(hpa["spec"]["scaleTargetRef"]["kind"] == "Rollout", "UI HPA must target the Rollout")
        require(hpa["spec"]["maxReplicas"] == (3 if environment == "dev" else 7), "HPA limits differ from the design")
    else:
        secrets = [source["secretRef"]["name"] for source in container["envFrom"] if "secretRef" in source]
        require(service + "-runtime" in secrets, "Missing ESO runtime credential reference")


def validate_bootstrap_order(documents, profile="architecture"):
    children = [document for document in documents if document["kind"] == "Application"]
    require(children, "Bootstrap has no child Applications")
    first_child_wave = min(sync_wave(document) for document in children)
    projects = {document["metadata"]["name"]: document for document in documents if document["kind"] == "AppProject"}
    for child in children:
        project = projects.get(child["spec"]["project"])
        require(project is not None, "Bootstrap omitted a child Application's AppProject")
        require(sync_wave(project) < sync_wave(child), "AppProject must precede its child Applications to avoid sync deadlock")
    namespaces = [document for document in documents if document["kind"] == "Namespace"]
    expected_namespaces = {"retail-dev"} | ({"retail-prod"} if profile == "architecture" else set())
    require({document["metadata"]["name"] for document in namespaces} >= expected_namespaces,
            "Bootstrap must create its enabled application namespaces")
    require(all(sync_wave(document) < first_child_wave for document in namespaces),
            "Bootstrap namespaces must precede child Applications")


def validate_profile_graph(profile, temporary):
    config = offline_target_config(profile)
    path = temporary / (profile + "-target.json")
    path.write_text(json.dumps(config), encoding="utf-8")
    documents = render(GITOPS / "bootstrap", [path, GITOPS / "versions.yaml"])
    validate_bootstrap_order(documents, profile)
    children = {document["metadata"]["name"]: document for document in documents if document["kind"] == "Application"}
    expected = set(platform_charts(config)) | {"retail-argocd-secrets", "retail-platform-resources", "retail-dev-configuration"}
    if profile == "architecture":
        expected.add("retail-prod-configuration")
    require(set(children) == expected, "Bootstrap platform Applications differ from the deployment profile")
    for name in platform_charts(config):
        relative = ["$values/" + value.relative_to(GITOPS).as_posix() for value in values_files(GITOPS, name, config)]
        require(children[name]["spec"]["sources"][0]["helm"]["valueFiles"] == relative,
                "Platform child lost its profile values: " + name)
    monitors = render(GITOPS / "platform-resources", [path])
    names = {document["metadata"]["name"] for document in monitors if document["kind"] == "PodMonitor"}
    require(len(names) == (5 if profile == "lab" else 10), "PodMonitors must follow the enabled environments")
    require((any(document.get("metadata", {}).get("name") == "velero" for document in monitors))
            == (profile == "architecture"), "Velero monitoring must follow profile enablement")


def validate_platform_profiles(cache):
    versions = yaml.safe_load((GITOPS / "versions.yaml").read_text(encoding="utf-8"))
    namespaces = {"argo-cd": "argocd", "argo-rollouts": "argo-rollouts", "external-secrets": "external-secrets",
                  "velero": "velero", "aws-load-balancer-controller": "kube-system", "metrics-server": "kube-system",
                  "cluster-autoscaler": "kube-system"}
    reports = {}
    for profile in ("lab", "architecture"):
        config = {"deploymentProfile": profile}
        documents = []
        for name in ("argo-cd", *platform_charts(config)):
            chart = cache / name / name
            require((chart / "Chart.yaml").is_file(), "Missing pinned platform chart cache: " + name)
            metadata = yaml.safe_load((chart / "Chart.yaml").read_text(encoding="utf-8"))
            require(str(metadata["version"]) == str(versions[name]["version"])
                    and str(metadata["appVersion"]).removeprefix("v") == str(versions[name]["appVersion"]).removeprefix("v"),
                    "Platform chart cache does not match versions.yaml: " + name)
            rendered = render(chart, values_files(GITOPS, name, config),
                              namespace=namespaces.get(name, "monitoring"),
                              release="argocd" if name == "argo-cd" else name, include_crds=True)
            if name == "aws-load-balancer-controller":
                validate_load_balancer_controller(rendered)
            if name == "argo-cd":
                require(not any(document["kind"] == "Secret" and document["metadata"]["name"] in {"argocd-secret", "argocd-redis"}
                                for document in rendered), "Argo CD chart must preserve the ESO credential Secrets")
                optional_components = {"dex-server", "notifications-controller", "applicationset-controller"}
                active_components = {document.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
                                     for document in rendered if document["kind"] in {"Deployment", "StatefulSet"}
                                     and document.get("spec", {}).get("replicas", 1) > 0}
                if profile == "lab":
                    require(not optional_components & active_components,
                            "Unused Argo CD components must be disabled in lab")
                else:
                    require(optional_components <= active_components,
                            "Architecture must enable Dex, Notifications and ApplicationSet controllers")
            documents.extend(rendered)
        if profile == "lab":
            require(any(document["kind"] == "CustomResourceDefinition"
                        and document["metadata"]["name"] == "podmonitors.monitoring.coreos.com" for document in documents),
                    "Lab must retain the PodMonitor CRD/operator for application monitoring")
            require(not any(document["kind"] == "PersistentVolumeClaim" for document in documents),
                    "Ephemeral lab monitoring must not provision persistent claims")
            prometheus = next(document for document in documents if document["kind"] == "Prometheus")
            require(prometheus["spec"]["retention"] == "6h"
                    and prometheus["spec"]["retentionSize"] == "512MB"
                    and "emptyDir" in prometheus["spec"]["storage"], "Lab metrics storage/retention is not bounded")
        reports[profile] = validate_budget(documents, profile)
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Application checkout; defaults to sibling retail-store-sample-app")
    parser.add_argument("--prod-source", type=Path, help="Production release chart checkout if its source commit differs from dev")
    parser.add_argument("--platform-cache", type=Path, help="Optional downloaded chart cache in <name>/<name> layout")
    args = parser.parse_args()
    global SOURCE
    if args.source:
        SOURCE = args.source.resolve()
    require((SOURCE / "src/ui/chart").is_dir(), "Supply the application chart checkout with --source PATH")
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        for profile in ("lab", "architecture"):
            validate_profile_graph(profile, temporary)
        actual_config = GITOPS / "config.yaml"
        actual = validate_activation(actual_config, temporary)
        config = temporary / "offline-target.json"
        config.write_text(json.dumps(offline_target_config()), encoding="utf-8")
        bootstrap = render(GITOPS / "bootstrap", [config, GITOPS / "versions.yaml"])
        validate_bootstrap_order(bootstrap)
        argocd_secrets = render(GITOPS / "argocd-secrets", [config])
        validate_repository_secrets(argocd_secrets, json.loads(config.read_text(encoding="utf-8")))
        private_config = offline_target_config()
        private_config["repositoryCredentials"] = {"enabled": True, "urlPrefix": "https://github.com/hoi3dkhoiche-byte/"}
        private_target = temporary / "private-repositories.json"
        private_target.write_text(json.dumps(private_config), encoding="utf-8")
        validate_repository_secrets(render(GITOPS / "argocd-secrets", [private_target]), private_config)
        storage = next(document for document in bootstrap if document["kind"] == "StorageClass")
        require(int(storage["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]) < 0, "StorageClass must exist before monitoring PVCs")
        grafana_secret = next(document for document in bootstrap if document["kind"] == "ExternalSecret" and document["metadata"]["name"] == "grafana-admin")
        require(int(grafana_secret["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]) < 0, "Grafana credentials must synchronize before the Grafana deployment")
        release = Path(directory) / "release.json"
        release.write_text(json.dumps({"release": {"enabled": True, "source_commit": "a" * 40, "backend_profile": "mysql-redis-opensearch"}, "images": {service: {"repository": "docker.io/hmannnguye/devops-retails-shop" + ("" if service == "ui" else "-" + service), "digest": "sha256:" + "b" * 64} for service in SERVICES}}))
        for environment in ("dev", "prod"):
            seed = validate_release(GITOPS / "environments" / environment / "images.yaml")
            if environment == "prod":
                require_production_acceptance(GITOPS, release_from_values(seed, allow_disabled=True))
            source = args.prod_source.resolve() if environment == "prod" and args.prod_source else SOURCE
            validate_source_checkout(source, seed)
            disabled = render(GITOPS / "applications", [config, GITOPS / "environments" / environment / "images.yaml"], {"environment": environment})
            if not seed["release"]["enabled"]:
                require(not any(document["kind"] == "Application" for document in disabled), "Empty seed must not deploy unverified images")
                require(not any(document["kind"] == "Ingress" for document in disabled), "Disabled release must not expose an absent UI service")
            else:
                validate_service_applications(disabled, environment, seed)
            if actual["deployment"]["enabled"]:
                render(GITOPS / "applications", [actual_config, GITOPS / "environments" / environment / "images.yaml"], {"environment": environment})
            docs = render(GITOPS / "applications", [config, release], {"environment": environment})
            validate_service_applications(docs, environment, json.loads(release.read_text(encoding="utf-8")))
            validate_ingress(docs, environment, offline_target_config())
            policy = next(document for document in docs if document["kind"] == "NetworkPolicy" and document["metadata"]["name"] == "default-deny")
            require(set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}, "Both directions must default deny")
            domain_config = dict(offline_target_config(), domain="example.test",
                                 certificateArn="arn:aws:acm:ap-southeast-1:111122223333:certificate/01234567-89ab-cdef-0123-456789abcdef")
            domain = render(GITOPS / "applications", [config, release], {
                "environment": environment, "domain": domain_config["domain"],
                "certificateArn": domain_config["certificateArn"]})
            validate_ingress(domain, environment, domain_config)
            for service in SERVICES:
                validate_source_chart(service, environment, source, seed["images"].get(service))
        render(GITOPS / "platform-resources", [config])
    if args.platform_cache:
        budgets = validate_platform_profiles(args.platform_cache)
        print("Rendered platform budgets: " + json.dumps(budgets, sort_keys=True))
    print("Activation guards passed; explicit offline fixture graph passed for five digest-pinned applications, both environments, routing, UI Rollout/HPA and bootstrap order. No account values were written or AWS resources deployed.")


if __name__ == "__main__":
    main()

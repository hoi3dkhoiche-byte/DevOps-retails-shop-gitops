"""Deployment profiles shared by preparation and offline Helm validation."""
from pathlib import Path

PROFILES = ("lab", "architecture")
OPTIONAL_CHARTS = frozenset({"loki", "alloy", "velero", "cluster-autoscaler"})
PLATFORM_CHARTS = ("argo-rollouts", "aws-load-balancer-controller", "external-secrets", "metrics-server",
                   "cluster-autoscaler", "kube-prometheus-stack", "loki", "alloy", "velero")


def deployment_profile(config):
    # Old outputs/configurations predate the lab profile and retain their full graph.
    profile = config.get("deploymentProfile", "architecture")
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ValueError("deploymentProfile must be lab or architecture")
    return profile


def platform_charts(config):
    return tuple(name for name in PLATFORM_CHARTS
                 if deployment_profile(config) == "architecture" or name not in OPTIONAL_CHARTS)


def values_files(root, chart, config):
    root = Path(root)
    files = [root / "platform" / (chart + ".yaml")]
    if deployment_profile(config) == "lab":
        overlay = root / "profiles/lab" / (chart + ".yaml")
        if chart == "argo-cd" or chart in platform_charts(config):
            if not overlay.is_file():
                raise ValueError("Missing lab platform overlay: " + chart)
            files.append(overlay)
    return files

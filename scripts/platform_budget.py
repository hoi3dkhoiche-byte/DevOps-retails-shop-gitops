"""Count rendered controllers and Prometheus-operator workloads before scheduling."""
from decimal import Decimal
import re


def quantity(value, cpu=False):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([A-Za-z]*)", str(value))
    if not match:
        raise ValueError("Unsupported resource quantity: " + str(value))
    number, suffix = Decimal(match[1]), match[2]
    if cpu:
        factors = {"": Decimal(1000), "m": Decimal(1), "u": Decimal("0.001"), "n": Decimal("0.000001")}
    else:
        factors = {"": 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
                   "k": 1000, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
    if suffix not in factors:
        raise ValueError("Unsupported resource suffix: " + suffix)
    return number * factors[suffix]


def checked_resources(container, label):
    resources = container.get("resources", {})
    for key in ("requests", "limits"):
        if not {"cpu", "memory"} <= resources.get(key, {}).keys():
            raise ValueError("Explicit CPU/memory requests and limits required: " + label)
    values = {key + "_" + resource: quantity(resources[key][resource], resource == "cpu")
              for key in ("requests", "limits") for resource in ("cpu", "memory")}
    for resource in ("cpu", "memory"):
        if not 0 < values["requests_" + resource] <= values["limits_" + resource]:
            raise ValueError("Invalid resource request/limit: " + label)
    return values


def reloader_resources(documents):
    arguments = [argument for document in documents if document.get("kind") == "Deployment"
                 for container in document.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                 for argument in container.get("args", []) if argument.startswith("--config-reloader-")]
    result = {"requests": {}, "limits": {}}
    for key in ("requests", "limits"):
        for resource in ("cpu", "memory"):
            prefix = "--config-reloader-" + resource + "-" + key[:-1] + "="
            matches = [argument[len(prefix):] for argument in arguments if argument.startswith(prefix)]
            if len(matches) != 1:
                raise ValueError("Rendered Prometheus operator must set explicit config-reloader resources")
            result[key][resource] = matches[0]
    return result


def rendered_pods(documents):
    reloaders = None
    pods = []
    for document in documents:
        kind, spec = document.get("kind"), document.get("spec", {})
        metadata = document.get("metadata", {})
        replicas = 1
        if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
            pod = spec.get("template", {}).get("spec", {})
            replicas = spec.get("replicas", 1) if kind != "DaemonSet" else 1
        elif kind == "CronJob":
            pod = spec["jobTemplate"]["spec"]["template"]["spec"]
        elif kind == "Pod":
            pod = spec
        elif kind in {"Prometheus", "Alertmanager"}:
            if reloaders is None:
                reloaders = reloader_resources(documents)
            # These CRs create StatefulSets at runtime. Include both the main
            # container and the operator-injected config reloader from its REAL flags.
            pod = {"nodeSelector": spec.get("nodeSelector", {}),
                   "containers": [{"name": kind.lower(), "resources": spec.get("resources", {})},
                                  {"name": "config-reloader", "resources": reloaders}],
                   "initContainers": spec.get("initContainers", [])}
            for container in spec.get("containers", []):
                existing = next((value for value in pod["containers"] if value["name"] == container["name"]), None)
                if existing is None:
                    pod["containers"].append(container)
                else:
                    existing.update(container)
            replicas = spec.get("replicas", 1)
        else:
            continue
        if not isinstance(replicas, int) or replicas < 0:
            raise ValueError("Rendered replica count must be a nonnegative integer")
        # A DaemonSet without a workload selector runs once on this management
        # node as well as on dev. Other platform controllers must target management.
        selector = pod.get("nodeSelector", {})
        if kind != "DaemonSet" and selector.get("workload") != "management":
            raise ValueError("Platform pod lacks management placement: " + metadata.get("name", ""))
        if selector.get("workload", "management") != "management":
            continue
        label = kind + "/" + metadata.get("name", "")
        regular = [checked_resources(container, label + "/" + container["name"])
                   for container in pod.get("containers", [])]
        init = [checked_resources(container, label + "/init/" + container["name"])
                for container in pod.get("initContainers", [])]
        if not regular:
            raise ValueError("Rendered workload has no containers: " + label)
        # Kubernetes schedules max(sum(app containers), largest sequential init).
        # Native sidecars are conservatively included in the running sum.
        totals = {}
        for field in regular[0]:
            running_sidecars, peak_init = Decimal(0), Decimal(0)
            for container, value in zip(pod.get("initContainers", []), init):
                if container.get("restartPolicy") == "Always":
                    running_sidecars += value[field]
                    peak_init = max(peak_init, running_sidecars)
                else:
                    peak_init = max(peak_init, running_sidecars + value[field])
            steady = sum(value[field] for value in regular) + running_sidecars
            totals[field] = max(steady, peak_init)
        pods.append({"workload": label, "replicas_on_management": replicas,
                     **{key: float(value) for key, value in totals.items()}})
    return pods


def validate_budget(documents, profile):
    pods = rendered_pods(documents)
    totals = {key: sum(Decimal(str(pod[key])) * pod["replicas_on_management"] for pod in pods)
              for key in ("requests_cpu", "requests_memory", "limits_cpu", "limits_memory")}
    largest = {key: max((Decimal(str(pod[key])) for pod in pods if pod["replicas_on_management"]), default=Decimal(0))
               for key in totals}
    report = {"profile": profile, "workloads": pods,
              "pods_on_management_including_hooks": sum(pod["replicas_on_management"] for pod in pods),
              "cpu_requests_millicores": float(totals["requests_cpu"]),
              "memory_requests_mib": float(totals["requests_memory"] / 1024**2),
              "memory_limits_mib": float(totals["limits_memory"] / 1024**2),
              "largest_pod_cpu_request_millicores": float(largest["requests_cpu"]),
              "largest_pod_memory_limit_mib": float(largest["limits_memory"] / 1024**2)}
    if profile == "lab":
        # A 2-vCPU/8-GiB node keeps at least 600m and 2Gi for EKS/system pods.
        # Count all Helm hooks and one largest-pod rolling replacement as headroom.
        if totals["requests_cpu"] + largest["requests_cpu"] > 1400:
            raise ValueError("Lab management CPU requests plus replacement exceed 1400m")
        if totals["requests_memory"] + largest["requests_memory"] > 4 * 1024**3:
            raise ValueError("Lab management memory requests plus replacement exceed 4Gi")
        if totals["limits_memory"] + largest["limits_memory"] > 6 * 1024**3:
            raise ValueError("Lab management memory limits plus replacement exceed 6Gi")
        if report["pods_on_management_including_hooks"] + 1 > 21:
            raise ValueError("Lab management platform pod budget leaves insufficient room for EKS add-ons")
        report["reserved_for_system_cpu_millicores"] = 600
        report["reserved_for_system_memory_mib"] = 2048
    return report

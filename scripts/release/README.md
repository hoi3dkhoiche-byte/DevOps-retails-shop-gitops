# Release and dev acceptance helpers

These Python standard-library helpers are copied from the application's `scripts/gitops/` package so the GitOps checkout can record dev acceptance without a second checkout. The source CI workflow remains in `hoi3dkhoiche-byte/DevOps-retails-shop`; it proposes future dev image updates after a complete successful CI/publish attempt.

From the GitOps repository root, after dev has synchronized and the UI Rollout is fully promoted:

```powershell
python scripts/release/accept_dev.py --images environments/dev/images.yaml --output environments/dev/accepted.json --context YOUR_EXPLICIT_EKS_CONTEXT --operator YOUR_NAME
```

The operator needs access to `retail-dev`, `kubectl`, Docker buildx and registry read access. The script verifies the five exact image digests/config IDs, workload readiness, active UI Rollout and application smoke endpoints. It discovers the HTTP Service port from the Kubernetes API, including the GitOps Services on port 8080. Missing, ambiguous or non-TCP Service ports stop acceptance. No acceptance proof is fabricated from CI results alone.

Review and commit the resulting `environments/dev/accepted.json`. Source workflow **Retail GitOps** can then propose production using the same accepted source SHA and five immutable digests. It requires Actions Variable `GITOPS_ENABLED=true` and Secret `GITOPS_TOKEN` in the source repository. The token needs Contents and Pull requests read/write access to this GitOps repository; keep its value outside Git. Production synchronization and UI promotion remain manual.

The underlying `release.py` validates provenance and manages generated image PRs. It targets only the fixed source and GitOps repositories in this project; it does not build images or apply Kubernetes resources.

Run the offline contract tests from this checkout:

```powershell
python -B -m unittest discover -s scripts/release -p 'test_*.py' -v
```

The tests use simulated GitHub, registry and Kubernetes responses and make no remote changes.

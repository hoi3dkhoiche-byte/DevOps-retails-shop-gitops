param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$AwsProfile,
    [ValidateNotNullOrEmpty()][string]$Python = "python",
    [ValidateNotNullOrEmpty()][string]$Context = "retail-lab-eks",
    [string]$Kubeconfig = ""
)
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    # This read-only guard must succeed before kubeconfig or cluster writes.
    & $Python scripts/prepare.py --check
    if ($LASTEXITCODE -ne 0) { throw "Prepare and explicitly activate config.yaml before bootstrap." }
    foreach ($command in @("aws", "kubectl", "helm")) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { throw "Required command missing: $command" }
    }
    $gitopsConfig = (& $Python -c "import json,yaml; print(json.dumps(yaml.safe_load(open('config.yaml', encoding='utf-8-sig'))))") | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Could not read the prepared target configuration." }
    $versions = (& $Python -c "import json,yaml; print(json.dumps(yaml.safe_load(open('versions.yaml', encoding='utf-8-sig'))))") | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Could not read pinned platform versions." }
    $rootApp = (& $Python -c "import json,yaml; print(json.dumps(yaml.safe_load(open('argocd/retail-root.yaml', encoding='utf-8-sig'))))") | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Could not read argocd/retail-root.yaml." }
    if ($rootApp.metadata.name -ne "retail-root" -or $rootApp.metadata.namespace -ne "argocd" -or
        $rootApp.spec.project -ne "default" -or
        $rootApp.spec.source.repoURL -ne $gitopsConfig.gitopsRepo -or
        $rootApp.spec.source.targetRevision -ne $gitopsConfig.gitopsRevision -or
        $rootApp.spec.source.path -ne "bootstrap" -or
        (($rootApp.spec.source.helm.valueFiles -join "|") -ne "../config.yaml|../versions.yaml") -or
        $rootApp.spec.source.helm.parameters -or $rootApp.spec.source.helm.values -or
        $rootApp.spec.source.helm.valuesObject -or $rootApp.spec.sources -or
        $rootApp.spec.destination.server -ne "https://kubernetes.default.svc" -or
        $rootApp.spec.destination.namespace -ne "argocd") {
        throw "The root Application does not match the prepared target repository/revision."
    }
    $credentialManifest = & helm template retail-argocd-secrets argocd-secrets --namespace argocd -f config.yaml
    if ($LASTEXITCODE -ne 0 -or -not $credentialManifest) { throw "Could not render Argo CD credential references." }
    $actualAccount = (& aws sts get-caller-identity --profile $AwsProfile --query Account --output text).Trim()
    if ($LASTEXITCODE -ne 0 -or $actualAccount -ne [string]$gitopsConfig.accountId) {
        throw "AWS profile does not match the configured account."
    }
    $clusterJson = & aws eks describe-cluster --name $gitopsConfig.clusterName --region $gitopsConfig.region --profile $AwsProfile --output json
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect the configured EKS cluster." }
    $cluster = ($clusterJson | ConvertFrom-Json).cluster
    $expectedArn = "arn:aws:eks:$($gitopsConfig.region):$($gitopsConfig.accountId):cluster/$($gitopsConfig.clusterName)"
    if ($cluster.arn -ne $expectedArn -or $cluster.status -ne "ACTIVE" -or $cluster.version -ne "1.36") {
        throw "The AWS target must be the configured active EKS 1.36 cluster."
    }
    if (-not $Kubeconfig) { $Kubeconfig = Join-Path $repoRoot ".local/kubeconfig.yaml" }
    if (-not [System.IO.Path]::IsPathRooted($Kubeconfig)) { $Kubeconfig = Join-Path $repoRoot $Kubeconfig }
    $Kubeconfig = [System.IO.Path]::GetFullPath($Kubeconfig)
    $kubeconfigDirectory = Split-Path -Parent $Kubeconfig
    if (-not (Test-Path -LiteralPath $kubeconfigDirectory)) {
        New-Item -ItemType Directory -Path $kubeconfigDirectory -Force | Out-Null
    }
    & aws eks update-kubeconfig --name $gitopsConfig.clusterName --region $gitopsConfig.region --profile $AwsProfile --alias $Context --kubeconfig $Kubeconfig
    if ($LASTEXITCODE -ne 0) { throw "Could not configure access to the EKS cluster." }
    $apiServer = & kubectl --kubeconfig $Kubeconfig --context $Context config view --minify -o 'jsonpath={.clusters[0].cluster.server}'
    if ($LASTEXITCODE -ne 0 -or $apiServer -ne $cluster.endpoint) { throw "Kubernetes context does not match the inspected EKS endpoint." }
    $allowed = & kubectl --kubeconfig $Kubeconfig --context $Context auth can-i '*' '*' --all-namespaces
    if ($LASTEXITCODE -ne 0 -or $allowed.Trim() -ne "yes") { throw "Bootstrap needs the administration EKS Access Entry." }
    # ESO comes first so Argo CD's initial credentials originate in Secrets Manager.
    & helm upgrade --install external-secrets external-secrets --repo $versions.'external-secrets'.repository --version $versions.'external-secrets'.version --namespace external-secrets --create-namespace --kubeconfig $Kubeconfig --kube-context $Context -f platform/external-secrets.yaml --wait --timeout 15m
    if ($LASTEXITCODE -ne 0) { throw "External Secrets Operator did not become ready." }
    $credentialManifest | & kubectl --kubeconfig $Kubeconfig --context $Context apply -f -
    if ($LASTEXITCODE -ne 0) { throw "Could not declare Argo CD credential references." }
    $credentialResources = @("externalsecret/argocd-admin", "externalsecret/argocd-redis")
    if ($gitopsConfig.repositoryCredentials.enabled -eq $true) {
        $credentialResources += "externalsecret/argocd-repositories"
    }
    & kubectl --kubeconfig $Kubeconfig --context $Context -n argocd wait @credentialResources --for=condition=Ready --timeout=5m
    if ($LASTEXITCODE -ne 0) { throw "Argo CD credentials have not synchronized from Secrets Manager." }
    & helm upgrade --install argocd argo-cd --repo $versions.'argo-cd'.repository --version $versions.'argo-cd'.version --namespace argocd --create-namespace --kubeconfig $Kubeconfig --kube-context $Context -f platform/argo-cd.yaml --wait --timeout 15m
    if ($LASTEXITCODE -ne 0) { throw "Argo CD installation did not become ready." }
    & kubectl --kubeconfig $Kubeconfig --context $Context apply -f argocd/retail-root.yaml
    if ($LASTEXITCODE -ne 0) { throw "Could not create the root GitOps Application." }
    Write-Host "Argo CD bootstrap completed. Check retail-root before enabling image PR automation."
} finally {
    Pop-Location
}

{{/* Shared activation contract. Keep identical in all four local charts. */}}
{{- define "retail.deploymentEnabled" -}}
{{- $profile := "architecture" -}}
{{- if hasKey .Values "deploymentProfile" -}}{{- $profile = .Values.deploymentProfile -}}{{- end -}}
{{- if or (not (kindIs "string" $profile)) (not (has $profile (list "lab" "architecture"))) -}}
{{- fail "deploymentProfile must be lab or architecture" -}}
{{- end -}}
{{- if and (eq $profile "lab") (eq (.Values.environment | default "") "prod") (.Values.release.enabled | default false) -}}
{{- fail "Lab profile forbids production workloads; use architecture after dev acceptance" -}}
{{- end -}}
{{- if not (kindIs "bool" .Values.deployment.enabled) -}}
{{- fail "deployment.enabled must be a boolean" -}}
{{- end -}}
{{- if .Values.deployment.enabled -}}
{{- $account := toString .Values.accountId -}}
{{- if or (not (regexMatch "^[0-9]{12}$" $account)) (eq $account "123456789012") (eq $account "000000000000") -}}
{{- fail "Activation requires the actual AWS accountId; run scripts/prepare.py --activate" -}}
{{- end -}}
{{- if or (ne .Values.region "ap-southeast-1") (ne .Values.clusterName "retail-lab-eks") -}}
{{- fail "Activation requires the configured Singapore retail-lab-eks target" -}}
{{- end -}}
{{- if not (regexMatch "^vpc-[0-9a-f]{8,17}$" (toString .Values.vpcId)) -}}
{{- fail "Activation requires a prepared vpcId" -}}
{{- end -}}
{{- range $key := list "nodeSecurityGroupId" "ordersSecurityGroupId" -}}
{{- if not (regexMatch "^sg-[0-9a-f]{8,17}$" (toString (index $.Values $key))) -}}
{{- fail (printf "Activation requires a prepared %s" $key) -}}
{{- end -}}
{{- end -}}
{{- $roles := .Values.roles | default dict -}}
{{- range $name := list "lb" "autoscaler" "rollouts" "eso-dev" "eso-prod" "eso-monitoring" "eso-argocd" "grafana" "loki" "velero" -}}
{{- $role := toString (index $roles $name | default "") -}}
{{- if or (not (regexMatch (printf "^arn:aws:iam::%s:role/.+$" $account) $role)) (contains "REPLACE" $role) -}}
{{- fail (printf "Activation requires the prepared same-account IAM role %s" $name) -}}
{{- end -}}
{{- end -}}
{{- if not .Values.adminCidrs -}}
{{- fail "Activation requires actual public administration IPv4 /32 CIDRs" -}}
{{- end -}}
{{- range $cidr := .Values.adminCidrs -}}
{{- if not (regexMatch "^([0-9]{1,3}\\.){3}[0-9]{1,3}/32$" $cidr) -}}
{{- fail "Administration access must use an IPv4 /32" -}}
{{- end -}}
{{- $octets := splitList "." (trimSuffix "/32" $cidr) -}}
{{- range $octet := $octets -}}
{{- if gt (atoi $octet) 255 -}}{{- fail "Invalid administration IPv4 address" -}}{{- end -}}
{{- end -}}
{{- $first := atoi (index $octets 0) -}}
{{- $second := atoi (index $octets 1) -}}
{{- if or (eq $first 0) (eq $first 10) (eq $first 127) (ge $first 224) (and (eq $first 172) (ge $second 16) (le $second 31)) (and (eq $first 100) (ge $second 64) (le $second 127)) (and (eq $first 198) (or (eq $second 18) (eq $second 19))) -}}
{{- fail "Administration CIDR must be an actual global public IPv4 /32" -}}
{{- end -}}
{{- range $prefix := list "169.254." "192.168." "192.0.2." "198.51.100." "203.0.113." -}}
{{- if hasPrefix $prefix $cidr -}}{{- fail "Placeholder/private administration CIDR cannot activate a cluster" -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- $buckets := .Values.buckets | default dict -}}
{{- range $name := list "logs" "backup" -}}
{{- if ne (index $buckets $name | default "") (printf "retail-lab-%s-%s" $account $name) -}}
{{- fail (printf "Activation requires the prepared %s bucket" $name) -}}
{{- end -}}
{{- end -}}
{{- if .Values.domain -}}
{{- if not (regexMatch (printf "^arn:aws:acm:ap-southeast-1:%s:certificate/[0-9a-f-]+$" $account) (toString .Values.certificateArn)) -}}
{{- fail "A controlled domain requires a prepared same-account Singapore ACM certificate" -}}
{{- end -}}
{{- end -}}
true
{{- end -}}
{{- end -}}

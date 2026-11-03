{{/* Restrict the optional credential template to this project's GitHub owner. */}}
{{- define "retail.repositoryCredentialsEnabled" -}}
{{- $credentials := .Values.repositoryCredentials | default dict -}}
{{- if not (kindIs "bool" $credentials.enabled) -}}
{{- fail "repositoryCredentials.enabled must be a boolean" -}}
{{- end -}}
{{- range $key, $value := $credentials -}}
{{- if not (has $key (list "enabled" "urlPrefix")) -}}
{{- fail "repositoryCredentials accepts metadata only; store username/password in Secrets Manager" -}}
{{- end -}}
{{- end -}}
{{- if $credentials.enabled -}}
{{- $prefix := "https://github.com/hoi3dkhoiche-byte/" -}}
{{- if ne $credentials.urlPrefix $prefix -}}
{{- fail "Repository credential URL prefix must be https://github.com/hoi3dkhoiche-byte/ including the trailing slash" -}}
{{- end -}}
{{- range $key := list "sourceRepo" "gitopsRepo" -}}
{{- $url := toString (index $.Values $key | default "") -}}
{{- if not (regexMatch "^https://github\\.com/hoi3dkhoiche-byte/[A-Za-z0-9][A-Za-z0-9_.-]*\\.git$" $url) -}}
{{- fail (printf "Repository credentials must cover the HTTPS .git URL in %s" $key) -}}
{{- end -}}
{{- end -}}
true
{{- end -}}
{{- end -}}

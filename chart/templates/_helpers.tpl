{{/* Returns .ref, failing unless it is pinned by digest. Usage: include "prison-pod.pinnedImage" (dict "name" "sandbox.image" "ref" .Values.sandbox.image) */}}
{{- define "prison-pod.pinnedImage" -}}
{{- $ref := required (printf "%s is required" .name) .ref -}}
{{- if not (regexMatch "@sha256:[a-f0-9]{64}$" $ref) -}}
{{- fail (printf "%s must be pinned by digest (<image>@sha256:<digest>), got %q" .name $ref) -}}
{{- end -}}
{{- $ref -}}
{{- end -}}

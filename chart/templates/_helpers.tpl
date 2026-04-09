{{- define "comdirect-firefly-sync.name" -}}
{{- .Chart.Name }}
{{- end }}

{{- define "comdirect-firefly-sync.fullname" -}}
{{- .Release.Name }}-{{ .Chart.Name }}
{{- end }}

{{- define "comdirect-firefly-sync.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "comdirect-firefly-sync.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "comdirect-firefly-sync.selectorLabels" -}}
app.kubernetes.io/name: {{ include "comdirect-firefly-sync.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

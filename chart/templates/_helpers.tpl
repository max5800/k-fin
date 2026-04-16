{{- define "k-fin.name" -}}
{{- .Chart.Name }}
{{- end }}

{{- define "k-fin.fullname" -}}
{{- .Release.Name }}-{{ .Chart.Name }}
{{- end }}

{{- define "k-fin.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "k-fin.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "k-fin.selectorLabels" -}}
app.kubernetes.io/name: {{ include "k-fin.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

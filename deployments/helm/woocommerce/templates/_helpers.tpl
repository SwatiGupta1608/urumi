{{- define "woocommerce.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "woocommerce.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "woocommerce.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "woocommerce.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Selector labels; call with (dict "ctx" . "component" "wordpress") */}}
{{- define "woocommerce.selectorLabels" -}}
app.kubernetes.io/name: {{ include "woocommerce.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "woocommerce.mariadb.fullname" -}}
{{- printf "%s-mariadb" (include "woocommerce.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "woocommerce.wordpress.fullname" -}}
{{- printf "%s-wordpress" (include "woocommerce.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "woocommerce.wordpress.pvcName" -}}
{{- printf "%s-wp-content" (include "woocommerce.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* repository:tag, plus @digest when pinned. Call with an image values map. */}}
{{- define "woocommerce.image" -}}
{{- if .digest }}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" .digest) }}
{{- fail (printf "image digest for %s must be sha256:<64 hex>" .repository) }}
{{- end }}
{{- printf "%s:%s@%s" .repository .tag .digest }}
{{- else }}
{{- printf "%s:%s" .repository .tag }}
{{- end }}
{{- end }}

{{- define "woocommerce.phpIniName" -}}
{{- printf "%s-php-ini" (include "woocommerce.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Rendered php.ini fragment from wordpress.phpIni. */}}
{{- define "woocommerce.phpIni" -}}
; Managed by the woocommerce Helm chart (wordpress.phpIni). Do not edit in-pod.
{{- range $k, $v := .Values.wordpress.phpIni }}
{{- if not (regexMatch "^[A-Za-z0-9_.]+$" $k) }}
{{- fail (printf "invalid php.ini directive name %q" $k) }}
{{- end }}
{{ $k }} = {{ $v }}
{{- end }}
{{- end }}

{{- define "woocommerce.ingress.rules" -}}
- host: {{ required "ingress.host is required" .Values.ingress.host | quote }}
  http:
    paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: {{ include "woocommerce.wordpress.fullname" . }}
            port:
              name: http
{{- end }}

{{- define "woocommerce.storageClass" -}}
{{- if . }}
storageClassName: {{ . | quote }}
{{- end }}
{{- end }}

{{/*
Environment shared by the WordPress container and the WP-CLI init Job, so
both render an identical wp-config.php from the image's wp-config-docker.php.
*/}}
{{- define "woocommerce.wordpress.env" -}}
- name: WORDPRESS_DB_HOST
  value: "{{ include "woocommerce.mariadb.fullname" . }}:{{ .Values.mariadb.service.port }}"
- name: WORDPRESS_DB_NAME
  value: {{ .Values.mariadb.database | quote }}
- name: WORDPRESS_DB_USER
  value: {{ .Values.mariadb.username | quote }}
- name: WORDPRESS_DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ required "mariadb.auth.existingSecret is required" .Values.mariadb.auth.existingSecret }}
      key: {{ .Values.mariadb.auth.userPasswordKey }}
- name: WORDPRESS_TABLE_PREFIX
  value: {{ .Values.wordpress.tablePrefix | quote }}
{{- range $env, $key := .Values.wordpress.auth.saltKeys }}
- name: {{ $env }}
  valueFrom:
    secretKeyRef:
      name: {{ required "wordpress.auth.existingSecret is required" $.Values.wordpress.auth.existingSecret }}
      key: {{ $key }}
{{- end }}
{{- if .Values.wordpress.debug }}
- name: WORDPRESS_DEBUG
  value: "1"
{{- end }}
- name: WORDPRESS_CONFIG_EXTRA
  value: |
    define('WP_HOME', {{ .Values.siteUrl | trimSuffix "/" | squote }});
    define('WP_SITEURL', {{ .Values.siteUrl | trimSuffix "/" | squote }});
    define('WP_MEMORY_LIMIT', {{ .Values.wordpress.phpMemoryLimit | squote }});
    define('DISALLOW_FILE_EDIT', true);
    {{- if hasPrefix "https://" .Values.siteUrl }}
    define('FORCE_SSL_ADMIN', true);
    {{- end }}
{{- end }}

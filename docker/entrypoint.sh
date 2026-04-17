#!/usr/bin/env bash
set -euo pipefail

cd /opt/project

# Default to container-specific site config unless caller overrides it
export MX3_SITE_YAML="${MX3_SITE_YAML:-/opt/project/mx3/config/site.docker.yaml}"

# Some mx3 scripts read config/site.yaml directly, so keep a live copy there
if [[ -f "$MX3_SITE_YAML" ]]; then
  cp "$MX3_SITE_YAML" /opt/project/mx3/config/site.yaml
fi

# Ensure common output root exists
mkdir -p /work

exec "$@"

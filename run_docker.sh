#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-mx3-docker}"
SPEC_DIR="${SPEC_DIR:?set SPEC_DIR to host SPEC2017 path}"
TRACE_DIR="${TRACE_DIR:?set TRACE_DIR to host traces path}"
WORK_DIR="${WORK_DIR:-$PWD}"

docker run --rm -it \
  -v "$SPEC_DIR:/mnt/spec2017" \
  -v "$TRACE_DIR:/mnt/traces" \
  -v "$WORK_DIR:/work" \
  -w /work \
  "$IMAGE_NAME" \
  bash

#!/bin/bash
# Scan an image for known vulnerabilities with Trivy, and write what it finds as
# Markdown (the publish job appends it to the run's summary):
#
#   scan-image.sh IMAGE [PLATFORM]      e.g. scan-image.sh bngsim-run:latest linux/amd64
#
# High and critical findings that have a fix, in the Debian packages and the
# Python packages alike. A report, not a gate: it exits 0 whatever it finds
# (the weekly rebuild from a freshly pulled base is what picks up the fixes),
# and non-zero only when the scan itself cannot run.
set -euo pipefail
IMAGE="${1:?the image to scan}"
PLATFORM="${2:-}"
# Trivy 0.76.1, by digest.
TRIVY=aquasec/trivy:0.76.1@sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c

dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
chmod 777 "$dir"
# One platform's image, where the image store holds an index (the containerd
# store, with attestations): Trivy reads one image from an archive.
if [ -n "$PLATFORM" ] && docker save --platform "$PLATFORM" -o "$dir/image.tar" "$IMAGE" 2>/dev/null; then
    :
else
    docker save -o "$dir/image.tar" "$IMAGE"
fi
docker run --rm -v "$dir":/scan "$TRIVY" image --input /scan/image.tar --scanners vuln \
    --severity HIGH,CRITICAL --ignore-unfixed --no-progress --quiet --format json --output /scan/report.json

python3 - "$dir/report.json" "$IMAGE" <<'EOF'
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
found = [(v["Severity"], v["VulnerabilityID"], v["PkgName"], v.get("InstalledVersion", ""), v.get("FixedVersion", ""))
         for r in report.get("Results", []) for v in (r.get("Vulnerabilities") or [])]
found = sorted(set(found), key=lambda f: (f[0] != "CRITICAL", f[1]))
print(f"#### {sys.argv[2]}: {len(found)} high or critical {'vulnerability' if len(found) == 1 else 'vulnerabilities'} with a fix")
if found:
    print()
    print("| severity | id | package | installed | fixed in |")
    print("|---|---|---|---|---|")
    for f in found[:50]:
        print("| " + " | ".join(f) + " |")
    if len(found) > 50:
        print(f"\nand {len(found) - 50} more")
print()
EOF

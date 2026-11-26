set shell := ["bash", "-euo", "pipefail", "-c"]

namespace := "prison-pod"
# Placeholder digest so the chart renders without pushed images.
fake := "x@sha256:" + sha256("")

# Run the operator's unit tests
test:
    cd operator && uv run pytest -q

# Lint the chart and check that it refuses unpinned images
lint:
    helm lint chart --set operator.image={{fake}} --set sandbox.image={{fake}}
    ! helm template chart --set operator.image=x:latest --set sandbox.image={{fake}} >/dev/null 2>&1

# Build both images, push them to ttl.sh (kept 24h) and record their digests in .images.yaml
push:
    #!/usr/bin/env bash
    set -euo pipefail
    id=$(head -c8 /dev/urandom | od -An -tx1 | tr -d ' \n')
    digest() {
      local ref="ttl.sh/prison-pod-$id-$1:24h"
      docker build -q --build-arg http_proxy --build-arg https_proxy --build-arg no_proxy \
        -t "$ref" "$1/" >/dev/null
      docker push -q "$ref" >/dev/null
      docker image inspect "$ref" | jq -r '.[0].RepoDigests[0]'
    }
    operator=$(digest operator)
    agent=$(digest agent)
    printf 'operator:\n  image: %s\nsandbox:\n  image: %s\n' "$operator" "$agent" | tee .images.yaml

# Apply the CRD (Helm installs it only once) and install or upgrade the chart with the pushed images
deploy *helm_args:
    kubectl apply --server-side -f chart/crds/
    helm upgrade --install prison-pod chart -n {{namespace}} --create-namespace -f .images.yaml {{helm_args}}

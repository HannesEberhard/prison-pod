set shell := ["bash", "-euo", "pipefail", "-c"]

namespace := "prison-pod"
# Where .github/workflows/images.yaml pushes to.
registry := "ghcr.io/hanneseberhard"
# Placeholder digest so the chart renders without pushed images.
fake := "x@sha256:" + sha256("")

# Run the operator's unit tests
test:
    cd operator && uv run pytest -q

# Lint the chart and check that it refuses unpinned images
lint:
    helm lint chart --set operator.image={{fake}} --set sandbox.image={{fake}}
    ! helm template chart --set operator.image=x:latest --set sandbox.image={{fake}} >/dev/null 2>&1

# Record the digests of the images CI built for a commit (default: main on GitHub) in .images.yaml
images commit=`jj log -r main@origin --no-graph -T commit_id`:
    #!/usr/bin/env bash
    set -euo pipefail
    shopt -s inherit_errexit
    digest() {
      local repo="{{registry}}/prison-pod-$1"
      local d
      d=$(docker buildx imagetools inspect "$repo:{{commit}}" | awk '/^Digest:/ {print $2}')
      [[ $d == sha256:* ]]
      echo "$repo@$d"
    }
    operator=$(digest operator)
    agent=$(digest agent)
    printf 'operator:\n  image: %s\nsandbox:\n  image: %s\n' "$operator" "$agent" | tee .images.yaml

# Install or upgrade the chart (CRD included) with the images from `just images`
deploy *helm_args:
    helm upgrade --install prison-pod chart -n {{namespace}} --create-namespace -f .images.yaml {{helm_args}}

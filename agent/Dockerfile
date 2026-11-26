FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM debian:bookworm-slim

ARG CLAUDE_VERSION=2.1.321
ARG MISE_VERSION=v2026.11.6

# The agent runs without root, so everything needing apt is baked in here.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash build-essential ca-certificates curl file git jq less make nano \
      netcat-openbsd openssh-client openssh-server \
      procps python3 python3-venv ripgrep unzip vim xz-utils \
 && rm -rf /var/lib/apt/lists/* \
 # Host keys are generated per sandbox by sshd.sh, never baked into the image.
 && rm -f /etc/ssh/ssh_host_* \
 && mkdir -p /run/sshd

# HOME is on the sandbox's PVC (mounted at /data by the operator), so user-level installs persist.
ENV HOME=/data/home
RUN useradd --uid 1000 --home-dir "$HOME" --no-create-home --shell /bin/bash agent

# Installed under a throwaway HOME so the binary doesn't live on the (not yet mounted) PVC.
RUN curl -fsSL https://claude.ai/install.sh | HOME=/tmp/claude-install bash -s "${CLAUDE_VERSION}" \
 && cp -L /tmp/claude-install/.local/bin/claude /usr/local/bin/claude \
 && rm -rf /tmp/claude-install

# Rootless installers: `uv tool install …`, `mise use -g node@22`, etc. install into $HOME.
COPY --from=uv /uv /uvx /usr/local/bin/
RUN curl -fsSL https://mise.run | MISE_VERSION="${MISE_VERSION}" MISE_INSTALL_PATH=/usr/local/bin/mise sh

# Declared here, not at the top: every RUN after an ARG sees it, so bumping it would rebuild the layers above.
# The binary goes to /usr/local/lib/gh; /usr/local/bin/gh is the wrapper that supplies the git token.
ARG GH_VERSION=2.103.3
RUN tarball="gh_${GH_VERSION}_linux_$(dpkg --print-architecture).tar.gz" \
 && cd /tmp \
 && curl -fsSLO "https://github.com/cli/cli/releases/download/v${GH_VERSION}/${tarball}" \
 && curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_checksums.txt" \
    | grep " ${tarball}\$" | sha256sum -c - \
 && mkdir -p /usr/local/lib/gh \
 && tar -xzf "$tarball" --strip-components=2 -C /usr/local/lib/gh "${tarball%.tar.gz}/bin/gh" \
 && rm "$tarball"

COPY --chmod=755 entrypoint.sh sshd.sh git-credential-secret render-system-prompt gh /usr/local/bin/
COPY system-prompt.md /usr/local/share/prison-pod/system-prompt.md
COPY sshd_config /etc/ssh/sandbox_sshd_config
# Login shells (e.g. over SSH) reset PATH in /etc/profile.
RUN echo 'export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:$PATH"' > /etc/profile.d/sandbox.sh

ENV PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:$PATH" \
    MISE_HIDE_UPDATE_WARNING=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_UPDATES=1 \
    GH_NO_UPDATE_NOTIFIER=1
USER 1000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

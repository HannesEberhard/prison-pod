# prison-pod

<p align="center">
  <img src="./assets/logo.svg" alt="Prison Pod Logo" width="160">
</p>

On-demand Claude Code sandboxes on Kubernetes. A `PrisonPod` names git repos, a git identity and a credentials Secret, and optionally a git token for private repos. A small [kopf](https://kopf.readthedocs.io) operator starts a pod that clones the repos and runs `claude remote-control`. You drive the session from [claude.ai/code](https://claude.ai/code) or the Claude app, where it appears under the PrisonPod's name.

Requirements:

- A Kubernetes cluster running Cilium with its L7 proxy enabled
- Helm
- A claude.ai subscription, for Remote Control

```yaml
apiVersion: prisonpod.fdr2026.dev/v1alpha1
kind: PrisonPod
metadata:
  name: fix-login-bug
spec:
  repos:                                    # cloned to /data/workspace/<name>
    - name: api
      url: https://github.com/org/api.git   # https
      ref: main                             # optional: branch or tag
    - name: web
      url: https://github.com/org/web.git
  credentialsSecretRef:
    name: claude-credentials                # same namespace, see Usage
  gitCredentialsSecretRef:                  # optional, for private repos, see Private repos
    name: git-credentials
  permissionMode: auto                      # auto (default), default, acceptEdits, plan, dontAsk, bypassPermissions
  git:                                      # git config --global user.name / user.email
    name: Your Name
    email: you@example.com
  egress:                                   # optional, see Network
    - host: registry.npmjs.org              # ports default to [443]
    - host: "*.githubusercontent.com"       # direct subdomains
    - host: github.com
      ports: [443, 22]
  ssh:                                      # optional, see SSH
    authorizedKeys:
      - ssh-ed25519 AAAA… you@laptop
```

```console
$ kubectl get pp
NAME            READY   REASON              REPOS     SESSION                                    AGE
fix-login-bug   True    SessionRegistered   api,web   https://claude.ai/code/session_01AbC…      3m
```

## How it works

```
PrisonPod ──owns──> PVC prison-pod-<name>                   mounted at /data
          ├─owns──> CiliumNetworkPolicy prison-pod-<name>   default deny, see Network
          └─owns──> Deployment prison-pod-<name>            1 replica, Recreate
                      └─ Pod (uid 1000, no capabilities)
                          ├─ init clone-<repo>   git clone → /data/workspace/<repo>, skipped if present
                          ├─ agent               claude remote-control, cwd /data/workspace, HOME=/data/home
                          └─ ssh (optional)      sshd on 127.0.0.1:2222
```

- **Reconcile:** a kopf timer runs as soon as a PrisonPod appears and then every 10 seconds. It creates the PVC if it's missing and server-side applies the network policy and then the Deployment, which creates them, corrects drift and rolls out spec changes. Applying unchanged objects changes nothing.
- **Status:** the `Ready` condition follows the pod: `PodPending`, `Cloning`, `CloneFailed` (with git's error), `AgentNotRunning`, `WaitingForSession`, then `SessionRegistered`, at which point the operator has read the session URL from the agent's log into `status.sessionURL`.
- **Restarts:** the workspace, `$HOME` and the session pointer live on the PVC. A new pod skips existing clones and resumes the previous session with `claude remote-control --continue`. If that fails (more than about 4 hours old, or none recorded), it starts a new session.
- **One session per sandbox:** a new session runs with `--capacity 1`, a resumed one reattaches to exactly one session, and the `Recreate` strategy ensures two pods never run side by side.
- **Editing:** `permissionMode`, `git`, `ssh` and the list of repos can be changed; the pod restarts and resumes its session. `egress` changes apply to the running pod. Repo entries and `credentialsSecretRef` are immutable. A removed repo's directory stays in the workspace.
- **Deletion:** deleting the PrisonPod garbage-collects the PVC, the network policy and the Deployment.

## Install

Every push to `main` builds both images and pushes them to `ghcr.io/hanneseberhard/prison-pod-{operator,agent}`, tagged with the commit SHA ([workflow](.github/workflows/images.yaml)). Once it has finished:

```sh
just images   # writes the digests of main's images to .images.yaml (or: just images <commit>)
just deploy   # helm upgrade --install into namespace prison-pod; extra arguments go to helm
```

The packages must be public (package settings on GitHub), or the cluster needs an image pull Secret. Or with your own images:

```sh
helm upgrade --install prison-pod chart/ -n prison-pod --create-namespace \
  --set operator.image=<registry>/prison-pod-operator@sha256:<digest> \
  --set sandbox.image=<registry>/prison-pod-agent@sha256:<digest>
```

- Images must be pinned by digest. The chart and the operator both reject tag-only references.
- The CRD is part of the chart's templates, so upgrades update it. `helm uninstall` keeps it, and with it every PrisonPod and its data; delete the CRD yourself to remove those.
- A new `sandbox.image` or `sandbox.resources` rolls every sandbox pod. Sessions resume, but a running Claude turn is interrupted.
- Other values: `sandbox.storageClass` (empty: the cluster default), `sandbox.storageSize` (5Gi, applies to new PVCs only) and `sandbox.runtimeClassName` (e.g. `gvisor`). See [chart/values.yaml](chart/values.yaml).

## Usage

Remote Control needs a claude.ai subscription login (Pro, Max, Team or Enterprise; Team and Enterprise need it enabled by an admin). It doesn't accept API keys or `claude setup-token` tokens. Log in once with `claude auth login`, then store the credentials together with the `oauthAccount` object from `~/.claude.json`; without that object, Remote Control can't determine your organization.

```sh
kubectl -n prison-pod create secret generic claude-credentials \
  --from-file=.credentials.json=$HOME/.claude/.credentials.json \
  --from-file=oauthAccount.json=<(jq .oauthAccount ~/.claude.json)
kubectl apply -f examples/hello-world.yaml   # namespace prison-pod
kubectl -n prison-pod get pp
kubectl -n prison-pod exec -it deploy/prison-pod-hello-world -- bash
```

## Private repos

Create a Secret with an access token and reference it as `spec.gitCredentialsSecretRef`:

```sh
kubectl -n prison-pod create secret generic git-credentials --from-literal=token=<access token>
```

- Keys: `token` (required), `username` (default `x-access-token`, which GitHub accepts for any token) and `host` (default `github.com`). Use `host` for another provider, with that provider's `username` (e.g. `oauth2` on GitLab).
- Repo URLs stay `https://`. A git credential helper sends the token only over https and only to `host`, never to other hosts.
- It's used by the clones, by Claude and by SSH sessions, so Claude can fetch, and push if the token allows it.
- For `github.com` tokens, `gh` uses it as well (a wrapper passes it as `GH_TOKEN`, so it's never stored on the volume). `gh` talks to `api.github.com`, which you have to allow in `spec.egress`; give the token the scopes your `gh` commands need (e.g. pull requests).
- The Secret is mounted into the pod, so Claude can read the token. Use a fine-grained token limited to the repos and permissions the sandbox needs (contents read, plus write to push) with a short expiry.
- The secret can be added to or removed from a running PrisonPod; the pod restarts and resumes its session.

## System prompt

Claude is told about its sandbox: the repos it works on, the installed tools, rootless installs into `$HOME`, what the network blocks and how to ask you to open a host (a `spec.egress` entry to add), and which tokens exist. The text is [agent/system-prompt.md](agent/system-prompt.md); change it there. At every start the entrypoint fills in the sandbox's name, namespace, repos and git token host and writes the result to `~/.claude/prison-pod.md`, which `~/.claude/CLAUDE.md` imports (`@prison-pod.md`). Your own notes in that `CLAUDE.md` stay. Claude can't see `spec.egress`, so it treats failing connections as blocked.

## SSH

`spec.ssh.authorizedKeys` adds an `ssh` sidecar: unprivileged sshd as `agent`, listening only on `127.0.0.1:2222`, sharing `/data` with Claude. Only plain public-key lines are accepted. You reach it through kubectl, so nothing is exposed, and SSH grants no access beyond what `kubectl exec` already gives. The host key is generated once per sandbox and kept on the PVC; a recreated PrisonPod gets a new one.

A dedicated key keeps your personal keys out of the cluster:

```sh
ssh-keygen -t ed25519 -N '' -C prison-pod -f ~/.ssh/prison-pod
```

Put its public key into `spec.ssh` (see the commented lines in [examples/hello-world.yaml](examples/hello-world.yaml)), or add it to a running sandbox. The pod restarts and resumes its session:

```sh
kubectl -n prison-pod patch pp hello-world --type merge \
  -p "{\"spec\":{\"ssh\":{\"authorizedKeys\":[\"$(cat ~/.ssh/prison-pod.pub)\"]}}}"
```

A merge patch replaces the whole list, so include any keys you want to keep. Removing `spec.ssh` removes the sidecar again.

Add to `~/.ssh/config`, with `KUBECONFIG` set (and another namespace if your sandboxes live elsewhere):

```
Host *.pp
  User agent
  IdentityFile ~/.ssh/prison-pod
  IdentitiesOnly yes
  ProxyCommand sh -c 'exec kubectl -n prison-pod exec -i "deploy/prison-pod-${0%%.pp}" -c ssh -- nc 127.0.0.1 2222' %h
```

Then `ssh hello-world.pp` works, and so do `scp`, `rsync`, VS Code Remote-SSH and `ssh -A` (git over SSH with your local keys). `IdentitiesOnly` stops ssh from offering every key in your agent first. `kubectl port-forward deploy/prison-pod-<name> 2222:2222` also works.

## Tools

There's no root, `sudo` or `apt`. The image ships `git`, `gh` (GitHub CLI), `build-essential`, `python3`, `jq`, `ripgrep`, `nano`, `vim` and `less`. Install anything else rootless into `$HOME`, which persists on the PVC:

```sh
uv tool install ruff      # Python tools and Python versions
mise use -g node@22       # Node, Go, Rust, Java, …
```

## Development

```sh
just test    # operator unit tests (uv run pytest)
just lint    # helm lint, and check that unpinned images are rejected
```

The operator is three modules: [manifests.py](operator/manifests.py) builds the PVC and Deployment, [status.py](operator/status.py) derives the status from the pod, and [main.py](operator/main.py) holds the kopf handlers and all API calls.

## Limitations

- **Credentials:** the Secret is a full account login; anyone who can read Secrets in the namespace can use the account. Each sandbox refreshes its own copy of the token on its PVC, which can leave the Secret's copy stale for sandboxes started later. If that happens, recreate the Secret.
- **Durability:** the PVC lives as long as the PrisonPod. With node-local storage such as `local-path`, losing the node loses the data. Push your work.
- **Git credentials:** one token per PrisonPod, for a single host, shared with everything in the sandbox. Only https is supported, not SSH deploy keys.
- **Repos:** clones are skipped when the directory exists, so a removed repo re-added under the same name with a different URL keeps the old checkout.
- **DNS:** only connections are restricted, not lookups, so a sandbox can still leak data through DNS queries.

## Network

Requires [Cilium](https://cilium.io) with its L7 proxy (`enable-l7-proxy`, the default). Each sandbox gets a `CiliumNetworkPolicy` that denies all ingress and all egress except:

- **DNS** to kube-dns, through Cilium's DNS proxy, which is how hostname rules learn the IPs behind a name.
- **The essential hosts** on 443: what Remote Control needs (`api.anthropic.com`, `claude.ai`, `claude.com`, `platform.claude.com`), set by the chart value `sandbox.essentialHosts`.
- **The repo hosts** on 443, taken from `spec.repos`.
- **`spec.egress`**: hostnames, or `*.domain` for its direct subdomains (one level, not the domain itself), each with TCP `ports` (default `[443]`).

Anything else, such as `uv tool install` (`pypi.org`, `files.pythonhosted.org`) or `mise` (`github.com`, `*.githubusercontent.com` and the tool's own hosts), has to be listed in `spec.egress`; changes apply to the running pod. Denied connections are dropped rather than refused, so clients hang until they time out; the system prompt tells Claude to use short timeouts and to ask you for a host instead of retrying. `kubectl exec`, `port-forward` and SSH go through the kubelet and aren't affected.

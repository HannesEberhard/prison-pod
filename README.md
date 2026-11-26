# prison-pod

On-demand Claude Code sandboxes on Kubernetes. A `PrisonPod` names git repos, a git identity and a credentials Secret. A small [kopf](https://kopf.readthedocs.io) operator starts a pod that clones the repos and runs `claude remote-control`. You drive the session from [claude.ai/code](https://claude.ai/code) or the Claude app, where it appears under the PrisonPod's name.

```yaml
apiVersion: prisonpod.fdr2026.dev/v1alpha1
kind: PrisonPod
metadata:
  name: fix-login-bug
spec:
  repos:                                    # cloned to /data/workspace/<name>
    - name: api
      url: https://github.com/org/api.git   # public, https
      ref: main                             # optional: branch or tag
    - name: web
      url: https://github.com/org/web.git
  credentialsSecretRef:
    name: claude-credentials                # same namespace, see Usage
  permissionMode: auto                      # auto (default), default, acceptEdits, plan, dontAsk, bypassPermissions
  git:                                      # git config --global user.name / user.email
    name: Your Name
    email: you@example.com
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
          └─owns──> Deployment prison-pod-<name>            1 replica, Recreate
                      └─ Pod (uid 1000, no capabilities)
                          ├─ init clone-<repo>   git clone → /data/workspace/<repo>, skipped if present
                          ├─ agent               claude remote-control, cwd /data/workspace, HOME=/data/home
                          └─ ssh (optional)      sshd on 127.0.0.1:2222
```

- **Reconcile:** a kopf timer runs as soon as a PrisonPod appears and then every 10 seconds. It creates the PVC if it's missing and server-side applies the Deployment, which creates it, corrects drift and rolls out spec changes. Applying an unchanged Deployment changes nothing.
- **Status:** the `Ready` condition follows the pod: `PodPending`, `Cloning`, `CloneFailed` (with git's error), `AgentNotRunning`, `WaitingForSession`, then `SessionRegistered`, at which point the operator has read the session URL from the agent's log into `status.sessionURL`.
- **Restarts:** the workspace, `$HOME` and the session pointer live on the PVC. A new pod skips existing clones and resumes the previous session with `claude remote-control --continue`. If that fails (more than about 4 hours old, or none recorded), it starts a new session.
- **One session per sandbox:** a new session runs with `--capacity 1`, a resumed one reattaches to exactly one session, and the `Recreate` strategy ensures two pods never run side by side.
- **Editing:** `permissionMode`, `git`, `ssh` and the list of repos can be changed; the pod restarts and resumes its session. Repo entries and `credentialsSecretRef` are immutable. A removed repo's directory stays in the workspace.
- **Deletion:** deleting the PrisonPod garbage-collects the PVC and the Deployment.

## Install

Images go to [ttl.sh](https://ttl.sh), an anonymous registry that keeps them for 24 hours:

```sh
just push     # builds both images, pushes them, writes their digests to .images.yaml
just deploy   # helm upgrade --install into namespace prison-pod; extra arguments go to helm
```

Or with your own registry:

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
kubectl -n <ns> create secret generic claude-credentials \
  --from-file=.credentials.json=$HOME/.claude/.credentials.json \
  --from-file=oauthAccount.json=<(jq .oauthAccount ~/.claude.json)
kubectl -n <ns> apply -f examples/hello-world.yaml
kubectl -n <ns> get pp
kubectl -n <ns> exec -it deploy/prison-pod-hello-world -- bash
```

## SSH

`spec.ssh.authorizedKeys` adds an `ssh` sidecar: unprivileged sshd as `agent`, listening only on `127.0.0.1:2222`, sharing `/data` with Claude. Only plain public-key lines are accepted. You reach it through kubectl, so nothing is exposed, and SSH grants no access beyond what `kubectl exec` already gives. The host key is generated once per sandbox and kept on the PVC; a recreated PrisonPod gets a new one. To try it, uncomment `ssh` in [examples/hello-world.yaml](examples/hello-world.yaml).

Add to `~/.ssh/config`, with `KUBECONFIG` set and the namespace of your sandboxes:

```
Host *.pp
  User agent
  ProxyCommand sh -c 'exec kubectl -n <ns> exec -i "deploy/prison-pod-${0%%.pp}" -c ssh -- nc 127.0.0.1 2222' %h
```

Then `ssh hello-world.pp` works, and so do `scp`, `rsync`, VS Code Remote-SSH and `ssh -A` (git over SSH with your local keys). `kubectl port-forward deploy/prison-pod-<name> 2222:2222` also works.

## Tools

There's no root, `sudo` or `apt`. The image ships `git`, `build-essential`, `python3`, `jq`, `ripgrep`, `nano`, `vim` and `less`. Install anything else rootless into `$HOME`, which persists on the PVC:

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
- **Public repos only:** there are no git credentials yet.
- **Repos:** clones are skipped when the directory exists, so a removed repo re-added under the same name with a different URL keeps the old checkout.
- **ttl.sh:** images expire after 24 hours; a pod scheduled onto a node without a cached copy then fails to pull. Run `just push && just deploy` again.

## Network

At runtime, sandboxes need `api.anthropic.com`, `claude.ai`, `claude.com`, `platform.claude.com` and the git hosts, plus whatever rootless installs download (e.g. `github.com`, `pypi.org`, `nodejs.org`). This list is the starting point for Cilium egress policies, which can select sandbox pods by the `prisonpod.fdr2026.dev/name` label.

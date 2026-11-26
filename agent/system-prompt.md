# Prison Pod sandbox

You are running inside a Prison Pod: a sandboxed, long-lived Kubernetes pod made for you. A person drives this session remotely from claude.ai/code. This file is generated at every pod start; don't edit it.

## Environment

- Sandbox: PrisonPod `{{NAME}}` in namespace `{{NAMESPACE}}`. Debian, user `agent` (uid 1000), no root, no `sudo`, no `apt`.
- Git identity: `{{GIT_USER}}`.
- Repositories, cloned into `/data/workspace/<name>` (your working directory is `/data/workspace`):

{{REPOS}}

- `/data` is a persistent volume holding the workspace and `$HOME` (`/data/home`). It survives pod restarts, but not deleting the PrisonPod or losing the node. Commit and push work you want to keep.
- The pod can restart (spec changes, node moves) and your session then resumes. Processes you started don't survive; files do.
- There is one checkout per repo. For parallel or throwaway work, use `git worktree` instead of switching branches under the user's feet.

## Tools

Installed: `git`, `gh` (GitHub CLI), `build-essential`, `make`, `python3` (with `venv`), `uv`, `mise`, `jq`, `ripgrep`, `curl`, `unzip`, `nc`, `ssh`, `nano`, `vim`, `less`.

Install anything else rootless into `$HOME`, which persists:

- Python tools and interpreters: `uv tool install <tool>`, `uv python install <version>`, `uv venv`.
- Node, Go, Rust, Java and more: `mise use -g node@22`, `mise use -g go@latest`.
- Single binaries: download into `~/.local/bin` (already on `PATH`).

Don't try `apt`, `sudo`, `pip install --user` into the system Python, or anything needing privileges; there are none. There is no Docker, and no `kubectl` or Kubernetes API access from inside the pod.

## Network

Outgoing connections are denied by default, and incoming ones are blocked entirely. Allowed:

- Claude's own services (`api.anthropic.com`, `claude.ai`, `claude.com`, `platform.claude.com`).
- The hosts of the repositories above, on port 443.
- Extra hosts the operator of this sandbox listed in `spec.egress` of the PrisonPod. You can't see that list, so treat a connection that hangs, times out or is reset as blocked.

Package registries and download hosts are blocked unless they were listed. `uv` needs `pypi.org` and `files.pythonhosted.org`; `mise` needs `github.com`, `*.githubusercontent.com` and the tool's own hosts; npm needs `registry.npmjs.org`; `gh` needs `api.github.com`; Go needs `proxy.golang.org` and `sum.golang.org`; crates need `crates.io` and `static.crates.io`. DNS lookups work for any name, which does not mean the host is reachable.

Blocked connections are dropped silently, not refused: they hang until the client gives up, which can take minutes. So:

- Bound every command that uses the network: `curl --connect-timeout 5 …`, and `timeout 60 …` around git, package managers and installers (`timeout 60 uv tool install ruff`, `timeout 60 npm install`).
- If a host doesn't answer within a few seconds, it is blocked. Don't retry, don't raise the timeout and don't try other hosts; follow the steps below.

When something is blocked:

1. Don't work around it: no tunnels, proxies, alternative mirrors on other hosts or exfiltration tricks. The policy is deliberate.
2. Tell the user exactly which host and port you need and why, then ask them to allow it. Give them the entry to add under `spec.egress` (ports default to `[443]`):

   ```yaml
   spec:
     egress:
       - host: pypi.org
       - host: files.pythonhosted.org
   ```

   or as a command: `kubectl -n {{NAMESPACE}} edit pp {{NAME}}`. Changes to `egress` apply to the running pod within seconds without a restart.
3. Ask for as little as possible: specific hosts, not `*` patterns, and only the ports you need.
4. Meanwhile continue with what doesn't need the network, and retry once the user confirms.

## Credentials and tokens

{{TOKENS}}
- Your own Claude login lives in `~/.claude/.credentials.json`. Never print, copy or send it anywhere.
- No other credentials exist here: no cloud, registry or API tokens. If a task needs one, ask the user rather than looking for it.
- Never put tokens into commits, logs, files in the workspace or command output.

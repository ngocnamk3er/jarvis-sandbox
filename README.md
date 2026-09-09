# jarvis-sandbox

The code-execution sandbox behind the Jarvis agent's `bash` tool and
`present_file`. One shared FastAPI service; every conversation gets its own
private directory and every bash command runs inside a throwaway Linux
namespace jail. Replaced OpenSandbox (its nested bwrap/userns isolation stopped
working once the host kernel set `apparmor_restrict_unprivileged_userns=1`).

## What it does

| route | who calls it | what it does |
|---|---|---|
| `POST /api/v1/sandbox/exec` | jarvis-backend `bash` tool | run one bash command for `thread_id`, return `{stdout, stderr, exit_code, timed_out}` |
| `GET  /api/v1/sandbox/read` | `present_file` / chat download chip | stream a file back out of the conversation's workspace |
| `POST /api/v1/sandbox/reset` | `/chat/stop`, conversation delete | `rm -rf` a conversation's tree |
| `GET  /api/v1/health` | probes | liveness |

Every route except `/health` requires the `X-Internal-Api-Key` header
(`INTERNAL_API_KEY`, shared with jarvis-backend). The service is never exposed
to the ingress.

## How the isolation works

On disk the service (root) keeps:

```
$DATA_ROOT/                         0711 root    traversable, NOT listable
$DATA_ROOT/<thread_id>/             0700 <uid>   the conversation "prefix"
$DATA_ROOT/<thread_id>/.last_used   0644 root    GC marker (agent never sees it)
$DATA_ROOT/<thread_id>/workspace/   0700 <uid>   the agent's /workspace
```

Each `exec` runs:

```
unshare --mount --pid --fork -- sh -c '
    mount --bind $DATA_ROOT/<tid>/workspace  /workspace   # only this thread's dir
    mount -t tmpfs -o mode=000               /data        # hide every other thread
    mount -t tmpfs                           /tmp /var/tmp /dev/shm   # private scratch
    mount -t proc proc                       /proc        # only own processes (PID ns)
    cd /workspace
    exec setpriv --reuid <uid> --regid <uid> --clear-groups --inh-caps=-all \
        bash -c "$COMMAND"                                 # drop root -> unprivileged
'
```

Three layers, each a backstop for the last:

1. **mount namespace** — a sibling thread's path literally doesn't exist in the view
2. **uid + `0700`** — even given the real path, the kernel denies a different uid
3. **no privileges** — the command can't `mount`/`unshare`/`chown` to climb out
   (`--inh-caps=-all`, and the image has every setuid bit stripped)

`present_file`/`/read` runs in the service (root, not jailed), so `runner.read_file`
does its own check: reject absolute paths + `..`, `.resolve()` (follows symlinks),
require the result to be inside the workspace.

Details / the Linux primitives involved: see the module docstring in
`app/services/runner.py`.

## Run it locally

The jail needs **root + `CAP_SYS_ADMIN`**, which `make dev` on a laptop doesn't
have. Two options:

### A. Dev mode, no isolation (fastest)

```bash
make install
cp .env.example .env          # keep UNSAFE_NO_JAIL=true
make dev                      # -> http://localhost:8003
```

`UNSAFE_NO_JAIL=true` runs commands with just `cwd` set — **no sandboxing at
all**. Fine for one developer on one machine; the service logs a warning on
every exec. Never set it anywhere shared.

Smoke test:

```bash
curl -s localhost:8003/api/v1/sandbox/exec \
  -H 'X-Internal-Api-Key: test-key' -H 'content-type: application/json' \
  -d '{"thread_id":"t1","command":"echo hi > note.txt && cat note.txt && pwd"}'
```

### B. Real jail, in Docker

```bash
docker build -t jarvis-sandbox .
docker run --rm -p 8003:8000 \
  --cap-add SYS_ADMIN --security-opt apparmor=unconfined \
  -e INTERNAL_API_KEY=test-key -e DATA_ROOT=/data \
  jarvis-sandbox
```

`--cap-add SYS_ADMIN` lets `unshare`/`mount` work; the container's own root user
then `setpriv`s each command down. This is the same posture as the k8s
Deployment.

### Wire it into jarvis-backend

In jarvis-backend's `.env`: `SANDBOX_SERVICE_URL=http://localhost:8003` and the
same `INTERNAL_API_KEY`.

## Config (`.env`)

| var | default | meaning |
|---|---|---|
| `INTERNAL_API_KEY` | — | shared secret with jarvis-backend |
| `DATA_ROOT` | `/data` | root of the per-conversation trees (k8s: the emptyDir mount) |
| `COMMAND_TIMEOUT_SECONDS` | `300` | per-command wall-clock limit; backend passes its own |
| `IDLE_GC_MINUTES` | `60` | delete a conversation dir untouched for this long |
| `UID_BASE` / `UID_RANGE` | `20000` / `40000` | per-thread uid pool for `setpriv` |
| `UNSAFE_NO_JAIL` | `false` | **dev only** — run with no isolation |

## Deploy (k8s)

Manifests in `jarvis-deploy/sandbox/`. The Deployment runs as **`runAsUser: 0`**
with `capabilities: {drop: [ALL], add: [SYS_ADMIN, SETUID, SETGID, CHOWN,
FOWNER, DAC_OVERRIDE, KILL]}` and an ephemeral `emptyDir` at `/data`. ArgoCD app
`jarvis-sandbox` auto-syncs it.

CI: `Jenkinsfile` builds the image, pushes to the GitLab registry, and bumps
`jarvis-deploy/sandbox/overlays/test/kustomization.yaml`. The Jenkins job exists
but its GitLab push webhook isn't wired yet — until then, build locally +
`minikube image load` + bump the overlay tag by hand (quote the tag — a bare
7-hex like `6264e35` is valid YAML scientific notation and kustomize rejects it).

## The image

`python:3.11-slim` + a data/analysis + document-generation toolchain baked in
(pandas, numpy, scipy, scikit-learn, matplotlib/seaborn/plotly, python-docx,
python-pptx, openpyxl, reportlab, fpdf2, pandoc, `uv` for ad-hoc installs).
~3.5 GB; `COPY app` is last so app-only changes rebuild in seconds. Every
setuid/setgid bit is stripped in the final layer.

## Known gaps

- Per-thread uid is `crc32(thread_id) % UID_RANGE` — collisions are possible,
  but the mount namespace is the real boundary, not the uid.
- The container's root filesystem (`/usr`, `/etc`, the service source at
  `/app`) is readable by a jailed command — no secrets there, just code.
- No `pip install` persistence: a pod restart is a fresh library set beyond the
  baked-in ones. `emptyDir` `/workspace` is also wiped on restart — expected;
  the agent regenerates files, and `present_file` links 404 after a restart.

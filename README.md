# jarvis-sandbox

The code-execution sandbox behind the Jarvis agent's `bash` tool and
`present_file`. **Every conversation gets its own dedicated k8s pod**, created
on demand; the pod *is* the isolation boundary. One container image, two roles:

| role | where it runs | what it does |
|---|---|---|
| **orchestrator** | one Deployment (`sandbox`), the Service jarvis-backend calls | maps `thread_id → pod`, creates/deletes agent pods, proxies `exec`/`read`. Talks to the k8s API. |
| **agent** | one pod per conversation, created by the orchestrator | runs the bash command in `/workspace`, streams back `{stdout, stderr, exit_code, timed_out}`, serves file reads for `present_file` |

---

## Theory — why it's built this way

**The job.** The Jarvis agent (an LLM) writes and runs arbitrary bash: `pip
install`, data crunching, generating `.docx`/`.pdf`/charts. That code is
*untrusted-ish* — not a paying attacker with a 0-day, but "the model does
something dumb, or a prompt injection tries something." It must not be able to
read another conversation's files, reach internal cluster services, exfiltrate
secrets, or wedge the box.

**Why a pod per conversation.** The previous design was one shared container
that wrapped every command in a per-`exec` `unshare`/`setpriv`/`mount`
namespace jail. It worked, but: one shared kernel, no network isolation (a
command could curl any in-cluster Service), no CPU/memory limits, and the
service itself ran as **root with `CAP_SYS_ADMIN`** — one namespace-escape from
a full container escape. Giving each conversation its own pod moves the
boundary to something k8s already enforces hard:

- own **network / PID / mount / IPC / UTS namespace** (every pod gets these)
- a **NetworkPolicy** with **no egress except DNS** — the command cannot reach
  the internet, any other pod or Service, or the node metadata IP
- **`runAsNonRoot`, drop ALL capabilities, `allowPrivilegeEscalation: false`,
  `seccompProfile: RuntimeDefault`, `readOnlyRootFilesystem: true`** (only
  `/workspace`, `/tmp`, `/var/tmp` are writable) — and the orchestrator needs
  no privileges either, just `pods` RBAC in one namespace
- **a fixed, offline toolchain** — the full data-analysis + doc-gen library set
  is baked into the image; `pip` / `uv` are removed, so the agent cannot
  install anything or pull data from a URL. No egress-driven cost, no
  surprise dependency
- per-pod **CPU / memory / ephemeral-storage limits** — a fork bomb or
  `malloc` loop hits only that conversation
- the pod is **deleted when the conversation ends** (or ages out) — a fresh
  disposable machine each time, no cross-conversation reuse to reason about

There is **no in-pod jail** — no `unshare`, `setpriv`, `mount --bind`, tmpfs
masking, per-thread uid. One pod = one conversation = thrown away after, so the
pod boundary replaces all of it. This is E2B's shape (an orchestrator handing
out per-session sandboxes) with a k8s pod standing in for a Firecracker
microVM.

**Why on-demand, not a warm pool.** `POOL_SIZE` defaults to `0`: no idle pods
are kept around. The first `bash` call of a conversation waits ~3–5s for its
pod to schedule and start; after that everything is instant. On a small node,
not parking idle pods (each holding ~256Mi of requests) is worth the one-time
wait. Raise `POOL_SIZE` to keep N pods pre-warmed if that first-call latency
matters.

**Why a hard cap and a TTL, not "keep exactly N alive".** Two knobs bound
resource use:

- `MAX_SANDBOXES` (default 6) — total agent pods never exceeds this. Past it,
  `exec` returns **503** and the backend surfaces "sandbox unavailable"; the
  conversation retries. This is a ceiling, not a target — the orchestrator
  never creates a pod nobody asked for.
- A pod is deleted when **either** it's been idle `IDLE_GC_MINUTES` (no bash
  call) **or** it has simply existed `SANDBOX_TTL_MINUTES` (a hard lifetime,
  even mid-conversation). Either way the conversation transparently gets a
  fresh pod on its next call — same as an idle reap or a pod restart. The
  workspace is ephemeral by design; the agent regenerates files.

**Why the orchestrator is nearly stateless.** `thread_id → pod` lives in the
pod's own **labels** (`jarvis.sandbox/thread`); the per-pod agent token is a
literal **env var** on the pod. The in-memory map is just a cache — on restart
the orchestrator lists the pods and re-adopts every running sandbox instead of
orphaning it. No database, no leader election beyond "run one replica".

**Trust between orchestrator and agent.** The orchestrator injects a random
`AGENT_TOKEN` per pod and replays it on every call; the agent rejects
mismatches. A bash command can read the token out of its own environ, but it's
scoped to that one pod — useless elsewhere. No long-lived shared secret ever
lands inside a sandbox.

---

## How to start

### Local dev (agent role only — no cluster)

Runs bash in a local directory. No isolation, no pool — fine for one
developer, same posture as the old `UNSAFE_NO_JAIL`. The orchestrator role
needs a real cluster.

```bash
make install-dev
cp .env.example .env          # SANDBOX_ROLE=agent, AGENT_TOKEN empty
make dev                      # -> http://localhost:8003
```

```bash
curl -s localhost:8003/api/v1/sandbox/exec \
  -H 'content-type: application/json' \
  -d '{"command":"echo hi > note.txt && cat note.txt && pwd"}'
```

`make test` runs the suite (`pytest`): agent runner (exec, timeout,
truncation, `read_file` containment — relative *and* `/workspace/...` forms)
and the pool logic against a fake k8s client (on-demand create, reuse,
capacity cap, idle GC, TTL, restart reconcile, warm pool when `POOL_SIZE>0`).

### In-cluster (the real thing)

Prereqs: a running cluster with `kubectl` context set, `jarvis` namespace,
`jarvis-secrets` holding `INTERNAL_API_KEY`. For NetworkPolicy enforcement the
cluster needs a policy-aware CNI (`minikube start --cni=calico`); without it
the policy is inert but harmless.

```bash
# 1. build + load the image (node runs cri-dockerd, so `docker images` on the
#    node is enough; --provenance=false avoids an OCI-manifest-list that
#    `minikube image load` mishandles)
docker build --provenance=false -t jarvis-sandbox:<tag> .
minikube image load jarvis-sandbox:<tag>

# 2. point the overlay at that tag (quote it — a bare 7-hex is YAML sci-notation)
cd ../jarvis-deploy && (cd sandbox/overlays/test && kustomize edit set image jarvis-sandbox=jarvis-sandbox:<tag>)

# 3. apply
kubectl apply -k sandbox/overlays/test
kubectl -n jarvis rollout status deploy/sandbox
```

Normally CI does 1–2: the `Jenkinsfile` builds one image, pushes it to the
in-cluster registry and bumps `sandbox/overlays/test/kustomization.yaml`;
ArgoCD app `jarvis-sandbox` then auto-syncs. The orchestrator reads its **own**
running image off the k8s API and starts agent pods with the same tag, so CI
only ever bumps the one Deployment image.

### From a stopped cluster

```bash
minikube start -p minikube                 # ArgoCD re-syncs everything
kubectl -n jarvis rollout restart deploy/keycloak   # it often crashloops once on DNS
# see the "Start Jarvis test cluster" note for the insecure-registry gotcha
```

### Verify

```bash
kubectl -n jarvis port-forward svc/sandbox 18080:8000 &
KEY=$(kubectl -n jarvis get secret jarvis-secrets -o jsonpath='{.data.INTERNAL_API_KEY}' | base64 -d)

curl -s localhost:18080/api/v1/admin/pods            # {threads:{}, pool_size:0, max_sandboxes:6, ttl_minutes:180, ...}
curl -s localhost:18080/api/v1/sandbox/exec -H "X-Internal-Api-Key: $KEY" \
  -H content-type:application/json \
  -d '{"thread_id":"t1","command":"python3 -c \"print(6*7)\" && whoami"}'
kubectl -n jarvis get pods -l app=jarvis-sandbox-agent -L jarvis.sandbox/state,jarvis.sandbox/thread
```

---

## How to use it (the request lifecycle)

jarvis-backend never changed — it calls the same three routes, keyed by
`thread_id`, behind `X-Internal-Api-Key`. What happens under each:

| route | caller | what the orchestrator does |
|---|---|---|
| `POST /api/v1/sandbox/exec` `{thread_id, command, timeout_seconds?}` | backend `bash` tool | **claim**: reuse this thread's pod if it's Ready, else create one (up to `MAX_SANDBOXES`, else 503), relabel it `claimed`/`thread=<id>`, then proxy the command to `http://<podIP>:8000` with the pod's `AGENT_TOKEN`. Returns `{stdout, stderr, exit_code, timed_out}`. |
| `GET /api/v1/sandbox/read?thread_id=&name=` | `present_file` / chat download chip | look up this thread's pod, proxy the read. `name` may be relative (`report.docx`) or the `/workspace/...` form — both resolve to the same file; `..` / paths outside `/workspace` / symlinks out are rejected. |
| `POST /api/v1/sandbox/reset` `{thread_id}` | `/chat/stop`, conversation delete | **release**: delete the pod, forget the mapping. Idempotent. |
| `GET /api/v1/health` | k8s probes | liveness |
| `GET /api/v1/admin/pods` | debugging, in-cluster only, no auth | current `thread → pod` map + effective limits |

Files a command writes under `/workspace` (or a plain relative path — same
thing) persist for the life of that pod, i.e. across bash calls in the
conversation, until an idle reap / the TTL / a `reset`.

---

## What improved vs. the shared container

| | old (shared container) | now (pod per conversation) |
|---|---|---|
| kernel | one, shared by all conversations | still shared (host kernel) — `SANDBOX_RUNTIME_CLASS=gvisor` for a per-sandbox kernel |
| network | pod netns shared; could reach every in-cluster Service | own netns + NetworkPolicy: **no egress except DNS** |
| toolchain | writable site-packages, `pip install` at runtime | fixed offline set baked in; pip / uv removed; read-only rootfs |
| resource limits | tmpfs sizes + wall-clock only | per-pod cpu / memory / ephemeral-storage limits |
| privileges | service ran as **root + `CAP_SYS_ADMIN`** | everything unprivileged, all caps dropped, seccomp RuntimeDefault |
| blast radius of an escape | root in a `CAP_SYS_ADMIN` pod | unprivileged uid in a locked-down, disposable pod |
| lifetime bound | idle GC only | idle GC **and** a hard TTL |

### Still open

- **Shared host kernel.** A kernel LPE still crosses pods. `SANDBOX_RUNTIME_CLASS=gvisor`
  closes it; needs the RuntimeClass installed on the cluster (follow-up overlay).
- **NetworkPolicy needs a policy-aware CNI** (`--cni=calico`). Inert otherwise —
  the read-only rootfs + stripped pip still block installs, but egress isn't
  cut until the CNI enforces it.
- **Image is ~3.5 GB** (offline: the whole toolchain plus NLTK corpora are
  baked in). Pulled once per node; keep `POOL_SIZE` small if you raise it.
- **First-call latency** ~3–5s (on-demand pod start) — raise `POOL_SIZE` to hide it.
- Workspace is an `emptyDir` — a pod restart / GC / TTL loses it, `present_file`
  links 404 after. Expected; the agent regenerates files.

---

## Config (`.env` / ConfigMap `sandbox-config`)

Consumed by the **orchestrator**. Agent pods get their env from the pod spec
the orchestrator writes ([app/orchestrator/podspec.py](app/orchestrator/podspec.py)),
derived from these same values — one source of truth.

| var | default | meaning |
|---|---|---|
| `SANDBOX_ROLE` | `agent` | `orchestrator` or `agent` |
| `INTERNAL_API_KEY` | — | shared secret with jarvis-backend (orchestrator checks it) |
| `POD_NAMESPACE` | `jarvis` | namespace agent pods are created in |
| `SANDBOX_IMAGE` | *(empty)* | agent-pod image; empty → orchestrator reads its own running image off the k8s API |
| `POOL_SIZE` | `0` | pods kept pre-warmed; `0` = pure on-demand |
| `MAX_SANDBOXES` | `6` | hard ceiling on total agent pods (past it → 503) |
| `IDLE_GC_MINUTES` | `30` | reap a sandbox no `exec` touched for this long |
| `SANDBOX_TTL_MINUTES` | `180` | hard per-pod lifetime, even if still active |
| `CLAIM_TIMEOUT_SECONDS` | `40` | wait budget for a new pod to become Ready |
| `COMMAND_TIMEOUT_SECONDS` | `300` | per-command wall-clock limit (agent) |
| `SANDBOX_CPU_*` / `SANDBOX_MEM_*` | see `.env.example` | agent pod requests/limits |
| `SANDBOX_WORKSPACE_SIZE` / `SANDBOX_TMP_SIZE` | `2Gi` / `1Gi` | emptyDir size caps |
| `SANDBOX_RUNTIME_CLASS` | *(empty)* | e.g. `gvisor` for a per-sandbox kernel |

Agent-only (set by the orchestrator, or `.env` locally): `WORKSPACE_DIR`
(`/workspace`), `AGENT_TOKEN` (per-pod bearer token; empty disables the check).

---

## Deploy (k8s)

Manifests in `jarvis-deploy/sandbox/`:

- `orchestrator.yaml` — ServiceAccount + Role (`pods: get/list/watch/create/
  delete/patch`) + RoleBinding, the `sandbox` Deployment (replicas 1,
  `Recreate` — exactly one orchestrator, or two race on pod management), the
  `sandbox` Service (name unchanged, so backend's `SANDBOX_SERVICE_URL` is
  untouched).
- `networkpolicy.yaml` — locks the agent pods down. **Needs a policy-aware CNI.**
- `configmap.yaml` — `sandbox-config`.

ArgoCD app `jarvis-sandbox` auto-syncs `sandbox/overlays/test`.

## The image

`python:3.11-slim` + a complete, **fixed** data-analysis and doc-generation
toolchain: numpy, pandas, scipy, statsmodels, pyarrow, scikit-learn, xgboost,
lightgbm, matplotlib, seaborn, plotly, nltk (+ common corpora bundled),
beautifulsoup4/lxml, python-docx, python-pptx, openpyxl, xlsxwriter, reportlab,
fpdf2, pillow, jinja2, and `pandoc`. Runs as uid 1000. **`pip` and `uv` are
removed** and the agent pod's rootfs is read-only, so the environment can't be
changed at runtime — to add a library, add it to the `Dockerfile` and rebuild.
Cache dirs (matplotlib, fontconfig, …) are pointed at `/tmp`. ~3.5 GB; `COPY
app` is last so app-only changes rebuild in seconds.

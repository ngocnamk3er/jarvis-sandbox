# jarvis-sandbox

The code-execution sandbox behind the Jarvis agent's `bash` tool and
`present_file`. **Every conversation gets its own dedicated pod** from a warm
pool; the pod *is* the isolation boundary. One container image, two roles:

| role | where it runs | what it does |
|---|---|---|
| **orchestrator** | one Deployment (`sandbox`), the Service jarvis-backend calls | keeps a warm pool of agent pods, maps `thread_id → pod`, proxies `exec`/`read`, deletes pods on `reset` / idle GC. Talks to the k8s API. |
| **agent** | one pod per conversation, created by the orchestrator | runs the bash command in `/workspace` and streams back `{stdout, stderr, exit_code, timed_out}`; serves file reads for `present_file` |

Replaced a single shared container that ran every conversation's commands in a
per-`exec` `unshare`/`setpriv`/`mount` namespace jail. That worked but shared
one kernel, had no network isolation, and put the whole service one
namespace-escape away from a root+`CAP_SYS_ADMIN` container. The pool model is
E2B's shape (an orchestrator handing out per-session sandboxes) with a k8s pod
in place of a Firecracker microVM.

## API (unchanged — jarvis-backend didn't change)

| route | caller | effect |
|---|---|---|
| `POST /api/v1/sandbox/exec` | backend `bash` tool | claim/reuse this `thread_id`'s pod, run one command in it |
| `GET  /api/v1/sandbox/read` | `present_file` / chat download chip | stream a file out of the conversation's workspace |
| `POST /api/v1/sandbox/reset` | `/chat/stop`, conversation delete | delete the conversation's pod |
| `GET  /api/v1/health` | probes | liveness |
| `GET  /api/v1/admin/pods` | debugging (in-cluster only) | current `thread → pod` map |

Every route except `/health` and `/admin/pods` requires the
`X-Internal-Api-Key` header (`INTERNAL_API_KEY`, shared with jarvis-backend).
The orchestrator is never exposed to the ingress.

## How the isolation works

```
jarvis-backend ──exec{thread_id,command}──▶ orchestrator (Deployment `sandbox`)
                                              │  claim: pick a warm pod, relabel
                                              │  it claimed + thread=<id>
                                              ▼
                                       agent pod  sbx-xxxx   (one per conversation)
                                         • own PID / net / mount / IPC / UTS ns
                                         • runAsNonRoot, drop ALL caps,
                                           allowPrivilegeEscalation: false,
                                           seccompProfile: RuntimeDefault
                                         • NetworkPolicy: internet yes,
                                           cluster + metadata IP no
                                         • cpu/mem limits; /workspace + /tmp
                                           are size-capped emptyDirs
                                         • deleted on reset / 60-min idle GC
```

There is **no in-pod jail** anymore — no `unshare`, `setpriv`, `mount --bind`,
tmpfs masking or per-thread uid. One pod holds exactly one conversation and is
thrown away after, so the pod's own boundary replaces all of it. The
orchestrator runs unprivileged too; it only needs `pods` RBAC in the `jarvis`
namespace.

**Pool state lives in the pods, not the orchestrator.** `thread_id` is a label
on the pod; the per-pod agent token is a literal env var. An orchestrator
restart lists the pods and re-adopts every running sandbox instead of orphaning
it.

`present_file` / `/read` is proxied to the agent, which still does its own
containment check on `name` (reject absolute + `..`, `.resolve()` to follow
symlinks, require the result inside `/workspace`).

### What improved vs. the shared container

| | old (shared container) | now (pod per conversation) |
|---|---|---|
| kernel | one, shared by all conversations | still shared (host kernel) — set `SANDBOX_RUNTIME_CLASS=gvisor` for a per-sandbox kernel |
| network | pod netns shared; could reach every in-cluster Service | own netns + NetworkPolicy: internet only, no cluster / metadata |
| resource limits | tmpfs sizes + wall-clock only | per-pod cpu / memory / ephemeral-storage limits |
| privileges | service ran as **root + `CAP_SYS_ADMIN`** | everything unprivileged, all caps dropped, seccomp RuntimeDefault |
| blast radius of an escape | root in a `CAP_SYS_ADMIN` pod | unprivileged uid in a locked-down, disposable pod |

### Still open

- **Shared host kernel.** A kernel LPE still crosses pods. The gVisor toggle
  (`SANDBOX_RUNTIME_CLASS`) closes this; needs the RuntimeClass installed on
  the cluster (planned as a follow-up overlay).
- **NetworkPolicy needs an enforcing CNI.** minikube's default bridge/kindnet
  ignores NetworkPolicy — start with `--cni=calico`. Without it the policy is
  inert (fail-open).
- **Capacity is a hard cap** (`MAX_SANDBOXES`). Past it, `exec` returns 503 and
  the backend surfaces "sandbox unavailable"; the conversation retries.
- **Image is ~3.5 GB.** Keep `POOL_SIZE` small (1–2) on a single node.
- Workspace is an `emptyDir` — a pod restart or GC loses it, and `present_file`
  links 404 after. Same as before; the agent regenerates files.

## Run it locally

Local dev runs the **agent** role only — bash in a directory, no cluster, no
isolation. Fine for one developer (the orchestrator needs a real cluster).

```bash
make install-dev
cp .env.example .env         # SANDBOX_ROLE=agent, AGENT_TOKEN empty
make dev                     # -> http://localhost:8003
```

Smoke test:

```bash
curl -s localhost:8003/api/v1/sandbox/exec \
  -H 'X-Agent-Token: ' -H 'content-type: application/json' \
  -d '{"command":"echo hi > note.txt && cat note.txt && pwd"}'
```

`make test` runs the suite (`pytest`): the agent runner (exec, timeout,
truncation, `read_file` containment) and the pool logic against a fake k8s
client (claim / reuse / refill / capacity cap / idle GC / restart reconcile).

## Config (`.env` / ConfigMap)

Orchestrator:

| var | default | meaning |
|---|---|---|
| `SANDBOX_ROLE` | `agent` | `orchestrator` or `agent` |
| `INTERNAL_API_KEY` | — | shared secret with jarvis-backend |
| `POD_NAMESPACE` | `jarvis` | namespace agent pods are created in |
| `SANDBOX_IMAGE` | *(empty)* | agent-pod image; empty → orchestrator reads its own running image off the k8s API |
| `POOL_SIZE` | `1` | warm pods kept Ready for instant claim |
| `MAX_SANDBOXES` | `6` | hard ceiling on total agent pods |
| `IDLE_GC_MINUTES` | `60` | delete a sandbox no `exec` touched for this long |
| `CLAIM_TIMEOUT_SECONDS` | `40` | wait budget for an on-demand pod to be Ready |
| `SANDBOX_CPU_*` / `SANDBOX_MEM_*` | see `.env.example` | agent pod resource requests/limits |
| `SANDBOX_WORKSPACE_SIZE` / `SANDBOX_TMP_SIZE` | `2Gi` / `1Gi` | emptyDir size caps |
| `SANDBOX_RUNTIME_CLASS` | *(empty)* | e.g. `gvisor` for a per-sandbox kernel |

Agent (set by the orchestrator in the pod spec, or by `.env` locally):

| var | default | meaning |
|---|---|---|
| `WORKSPACE_DIR` | `/workspace` | the one directory the command sees |
| `COMMAND_TIMEOUT_SECONDS` | `300` | per-command wall-clock limit |
| `AGENT_TOKEN` | *(empty)* | per-pod bearer token; empty disables the check (local dev) |

## Deploy (k8s)

Manifests in `jarvis-deploy/sandbox/`:

- `orchestrator.yaml` — ServiceAccount + Role (`pods` verbs) + RoleBinding,
  the `sandbox` Deployment (replicas 1, `Recreate`), the `sandbox` Service.
- `networkpolicy.yaml` — locks the agent pods down (internet yes, cluster no).
  **Needs a NetworkPolicy-enforcing CNI** (calico).
- `configmap.yaml` — `sandbox-config`, consumed by the orchestrator; agent
  pods derive their env from the same values.

ArgoCD app `jarvis-sandbox` auto-syncs `sandbox/overlays/test`. CI
(`Jenkinsfile`) builds one image and bumps the overlay's image tag — the
orchestrator propagates that tag to agent pods automatically.

## The image

`python:3.11-slim` + a data/analysis + document-generation toolchain
(pandas, numpy, scipy, scikit-learn, matplotlib/seaborn/plotly, python-docx,
python-pptx, openpyxl, reportlab, fpdf2, pandoc, `uv`). Runs as uid 1000,
primary group 0 — site-packages is made group-writable so the agent's
`pip install <pkg>` still works. ~3.5 GB; `COPY app` is last so app-only
changes rebuild in seconds.

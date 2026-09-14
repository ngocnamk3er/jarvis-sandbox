# jarvis-sandbox

The code-execution sandbox behind the Jarvis agent's `bash` tool and
`present_file`. **Every conversation gets its own dedicated k8s pod**, created
on demand; the pod *is* the isolation boundary.

This repo builds the pod **image** (a fixed data-analysis + doc-generation
toolchain, plus `app/agent/agentsandbox_server.py` — a small FastAPI server
exposing `GET /`, `POST /execute`, `POST /upload`, `GET /download/<path>`).
Pod *lifecycle* — creating one per conversation, routing requests to it,
tearing it down — is owned by [kubernetes-sigs/agent-sandbox](https://agent-sandbox.sigs.k8s.io/docs/),
not by anything in this repo. jarvis-backend talks to agent-sandbox's
controller directly (see `app/agents/tools/sandbox_manager.py` in the
jarvis-backend repo); there is no orchestrator Deployment here anymore.

> jarvis-sandbox used to ship its own orchestrator (a Deployment that
> created/tracked/deleted agent pods itself). That was decommissioned
> 2026-09-14 in favor of agent-sandbox — see
> [AGENTSANDBOX-MIGRATION.md](AGENTSANDBOX-MIGRATION.md) for the full
> history of that move, and [DEPLOY-STANDALONE.md](DEPLOY-STANDALONE.md)
> if you're setting this up somewhere new.

---

## Theory — why it's built this way

**The job.** The Jarvis agent (an LLM) writes and runs arbitrary bash: `pip
install`, data crunching, generating `.docx`/`.pdf`/charts. That code is
*untrusted-ish* — not a paying attacker with a 0-day, but "the model does
something dumb, or a prompt injection tries something." It must not be able
to read another conversation's files, reach internal cluster services,
exfiltrate secrets, or wedge the box.

**Why a pod per conversation.** Giving each conversation its own pod moves
the isolation boundary to something k8s already enforces hard:

- own **network / PID / mount / IPC / UTS namespace** (every pod gets these)
- a **NetworkPolicy** with **no egress except DNS** — the command cannot
  reach the internet, any other pod or Service, or the node metadata IP
- **`runAsNonRoot`, drop ALL capabilities, `allowPrivilegeEscalation: false`,
  `seccompProfile: RuntimeDefault`, `readOnlyRootFilesystem: true`** (only
  `/workspace`, `/tmp`, `/var/tmp` are writable)
- **a fixed, offline toolchain** — the full data-analysis + doc-gen library
  set is baked into the image; `pip` / `uv` are removed, so the agent cannot
  install anything or pull data from a URL
- per-pod **CPU / memory / ephemeral-storage limits** — a fork bomb or
  `malloc` loop hits only that conversation
- the pod is **deleted when the conversation ends** (or ages out) — a fresh
  disposable machine each time, no cross-conversation reuse to reason about

There is **no in-pod jail** — no `unshare`, `setpriv`, `mount --bind`, tmpfs
masking. One pod = one conversation = thrown away after, so the pod boundary
replaces all of it. This is E2B's shape (a controller handing out
per-session sandboxes) with a k8s pod standing in for a Firecracker microVM
— which is exactly what agent-sandbox's `Sandbox`/`SandboxClaim`/
`SandboxTemplate`/`SandboxWarmPool` CRDs implement generically, instead of
this repo reimplementing that lifecycle logic itself.

**Reattaching to the same pod across a conversation's `bash` calls.**
jarvis-backend labels each `SandboxClaim` with the conversation's
`thread_id` at creation, and looks it up the same way on later calls
(`list_all_sandboxes(label_selector=...)`) — Kubernetes is the source of
truth for "does this conversation have a sandbox", no separate database.
See that module's docstring for the full design writeup.

---

## How to start

### Local dev — no cluster

Runs the same server a real agent-sandbox pod runs, just against a local
directory instead of a k8s-managed `emptyDir`. No isolation — fine for one
developer.

```bash
make install-dev
cp .env.example .env
make dev                      # -> http://localhost:8003
```

```bash
curl -s localhost:8003/execute \
  -H 'content-type: application/json' \
  -d '{"command":"echo hi > note.txt && cat note.txt && pwd"}'
```

`make test` runs the suite (`pytest`): the exec/timeout/truncation logic and
`read_file`/`write_file` path containment (relative *and* `/workspace/...`
forms) in `app/agent/runner.py`.

### Building the image for a real cluster

```bash
# --provenance=false avoids an OCI-manifest-list some registries mishandle
docker build --provenance=false -f Dockerfile.agentsandbox \
  --build-arg BASE_IMAGE=<current-tag-of-the-plain-Dockerfile-build> \
  -t <your-registry>/jarvis-sandbox:agentsandbox-<tag> .
```

`Dockerfile.agentsandbox` is a thin layer on top of the plain `Dockerfile`
(same toolchain, just swaps the entrypoint) — build the base image first
with the plain `Dockerfile`, then this one on top with `--build-arg
BASE_IMAGE=...` pointing at it. See
[AGENTSANDBOX-MIGRATION.md](AGENTSANDBOX-MIGRATION.md) for the full
Template/WarmPool YAML this image is meant to run under, and
[DEPLOY-STANDALONE.md](DEPLOY-STANDALONE.md) for a complete from-zero
walkthrough (install agent-sandbox itself, build this image, wire up RBAC)
on a cluster that's never had any of this before.

---

## What the image actually serves

`app/agent/agentsandbox_server.py` — matches agent-sandbox's own runtime
contract (**not** jarvis-sandbox's own protocol from before the cutover):

| route | what it does |
|---|---|
| `GET /` | health check (readiness/liveness probe, and how the SDK knows a claimed pod is up) |
| `POST /execute` `{command}` | runs the command in `/workspace`, returns `{stdout, stderr, exit_code}` |
| `POST /upload` (multipart, field `file`) | writes the uploaded content under `/workspace` |
| `GET /download/<path>` | reads a file back out from `/workspace` |

Files written under `/workspace` (or a plain relative path — same thing)
persist for the life of that pod, i.e. across `bash` calls in the same
conversation, until agent-sandbox reclaims it (idle GC / TTL / an explicit
`delete_sandbox()` from jarvis-backend's `reset()`).

---

## The image

`python:3.11-slim` + a complete, **fixed** data-analysis and doc-generation
toolchain: numpy, pandas, scipy, statsmodels, pyarrow, scikit-learn, xgboost,
lightgbm, matplotlib, seaborn, plotly, nltk (+ common corpora bundled),
beautifulsoup4/lxml, python-docx, python-pptx, openpyxl, xlsxwriter, reportlab,
fpdf2, pillow, jinja2, and `pandoc`. Runs as uid 1000. **`pip` and `uv` are
removed** and the pod's rootfs is read-only, so the environment can't be
changed at runtime — to add a library, add it to the `Dockerfile` and
rebuild. Cache dirs (matplotlib, fontconfig, …) are pointed at `/tmp`.
~3.5 GB; `COPY app` is last so app-only changes rebuild in seconds.

## Config (`.env`)

| var | default | meaning |
|---|---|---|
| `WORKSPACE_DIR` | `/workspace` | the one directory the command sees — an emptyDir in k8s (mounted by the SandboxTemplate), any writable dir locally |

Everything else (how many pods, warm-pool size, resource limits, TTL, RBAC,
NetworkPolicy) lives in the `SandboxTemplate`/`SandboxWarmPool` YAML and
jarvis-backend's `AGENTSANDBOX_NAMESPACE`/`AGENTSANDBOX_WARMPOOL` settings —
not in this repo. See AGENTSANDBOX-MIGRATION.md.

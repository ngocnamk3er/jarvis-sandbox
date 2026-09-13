# Migrating jarvis-sandbox to kubernetes-sigs/agent-sandbox

Plan + verified setup for replacing jarvis-sandbox's own orchestrator/pod-pool
with [kubernetes-sigs/agent-sandbox](https://agent-sandbox.sigs.k8s.io/docs/).
Phase 1 is done and verified live. Phase 2 steps A, B, and C are done, and as
of 2026-09-13 all three are **committed and shipped to both repos' main
branches**, having gone through the real Jenkins → ArgoCD pipeline into the
production `jarvis` namespace:

- `jarvis-sandbox` commit `33767e2` (Phase 2 A+C — hardened adapter image +
  `/upload`/`/download` filesystem endpoints) — live in the real `sandbox`
  Deployment's pod.
- `jarvis-backend` commit `d26a1b9` (Phase 2 B — `sandbox_manager_agentsandbox.py`)
  — live in the real `backend` Deployment's pod.

**This is code being live, not behavior changing.** Nothing here is on any
live request path: `sandbox_manager_agentsandbox.py` isn't imported by any
tool, `agentsandbox_server.py` only runs under `Dockerfile.agentsandbox`
which no Jenkinsfile builds (the default `Dockerfile` does, unchanged
entrypoint), and jarvis-backend has no RBAC to talk to agent-sandbox's CRDs.
The two production Deployments behave exactly as before this push — only
their images now happen to contain this additional, inert code (plus two
small genuinely-shared additions: `runner.py`'s `write_file()`/
`_resolve_in_workspace()` helpers, and `python-multipart` in
`requirements.txt`). The cutover (step F) — the point where any of this
starts actually being called — is still not started, deliberately, left for
whoever's ready to do it with eyes open.

## Phase 1 — verified working

The existing jarvis-sandbox image (pandas/numpy/matplotlib/python-docx/
pandoc, ~3.5GB, already built and pushed) runs **unmodified** as the base
for a custom agent-sandbox runtime — confirmed live with `pandas` executing
correctly inside a pod provisioned by agent-sandbox's own controller. This
ran alongside the real jarvis-sandbox deployment the whole time (own
namespace `agent-sandbox-system`, own CRDs, own SandboxTemplates in
`default`) — nothing in Phase 1 touched the `jarvis` namespace or the
production sandbox orchestrator.

### Files this added to the repo

- `Dockerfile.agentsandbox` — thin layer on top of the real jarvis-sandbox
  image, swaps the entrypoint for `app/agent/agentsandbox_server.py`.
- `app/agent/agentsandbox_server.py` — FastAPI server matching the
  *installed* `k8s-agent-sandbox` SDK's actual wire format (see "SDK vs
  docs" below). Reuses `app.agent.runner.exec_command()`, the same
  function jarvis-sandbox's own agent uses. Not wired into `app.main`'s
  `SANDBOX_ROLE` dispatch yet — see Phase 2.

### 1. Install

Needs an existing cluster with `kubectl` pointed at it (this was run
against the same minikube cluster jarvis's own test stack uses — installing
alongside is safe, separate namespace/CRDs).

```bash
kubectl config current-context   # sanity check before doing anything

VERSION=$(curl -s https://api.github.com/repos/kubernetes-sigs/agent-sandbox/releases/latest | jq -r '.tag_name')

# Controller + core CRD
kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/sandbox.yaml
# Extension CRDs: SandboxTemplate / SandboxClaim / SandboxWarmPool
kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/extensions.yaml
kubectl -n agent-sandbox-system rollout status deployment/agent-sandbox-controller --timeout=90s

# sandbox-router — routes client requests to the right Sandbox pod.
# NOTE: disables router auth, local-test only — Phase 2 needs this on.
curl -sSL https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/refs/tags/${VERSION}/clients/python/agentic-sandbox-client/sandbox-router/sandbox_router.yaml \
  | sed 's|${ROUTER_IMAGE}|us-central1-docker.pkg.dev/k8s-staging-images/agent-sandbox/sandbox-router:latest-main|g' \
  | sed '/ALLOW_UNAUTHENTICATED_ROUTER/{n;s/value: "false"/value: "true"/}' \
  | kubectl -n agent-sandbox-system apply -f -
kubectl -n agent-sandbox-system rollout status deployment/sandbox-router-deployment --timeout=90s
```

#### Sanity check: install worked at all

Not required for anything downstream, just confirms the CRDs + controller
are actually functional before building anything:

```bash
curl -sSL https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/refs/tags/${VERSION}/clients/python/agentic-sandbox-client/python-sandbox-template.yaml \
  | sed -e 's|${SANDBOX_NAMESPACE}|default|g' -e 's|${SANDBOX_TEMPLATE_NAME}|test-sandbox-template|g' \
  | kubectl apply -f -

kubectl get sandboxtemplate test-sandbox-template -n default   # should just show up, no pod created yet
```

### 2. Sanity check with the generic quickstart (before touching jarvis's own image)

Validates the whole pipeline — controller, router, SDK, Tunnel mode — with
agent-sandbox's own stock `python-sandbox` image, before introducing any
jarvis-specific code. Worth doing first; if this doesn't work, the
jarvis-specific build won't either and you've isolated the problem.

```bash
export SANDBOX_NAMESPACE=default
export SANDBOX_TEMPLATE_NAME=python-sandbox-template
export SANDBOX_WARMPOOL_NAME=python-sandbox-pool

# Template
curl -sSL https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/refs/tags/${VERSION}/clients/python/agentic-sandbox-client/python-sandbox-template.yaml \
  | sed -e "s|\${SANDBOX_NAMESPACE}|${SANDBOX_NAMESPACE}|g" -e "s|\${SANDBOX_TEMPLATE_NAME}|${SANDBOX_TEMPLATE_NAME}|g" \
  | kubectl apply -f -

# WarmPool — the YAML defaults to replicas: 0 (empty pool); bump to 1 so a
# pod actually gets created.
curl -sSL https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/refs/tags/${VERSION}/clients/python/agentic-sandbox-client/python-sandbox-warmpool.yaml \
  | sed -e "s|\${SANDBOX_NAMESPACE}|${SANDBOX_NAMESPACE}|g" -e "s|\${SANDBOX_TEMPLATE_NAME}|${SANDBOX_TEMPLATE_NAME}|g" -e "s|\${SANDBOX_WARMPOOL_NAME}|${SANDBOX_WARMPOOL_NAME}|g" -e 's/replicas: 0/replicas: 1/' \
  | kubectl apply -f -

kubectl get pods -n default -w   # wait for python-sandbox-pool-xxx to hit 1/1 Running

pip install k8s-agent-sandbox
```

```python
# save as test_sandbox.py, then: python3 test_sandbox.py
from k8s_agent_sandbox import SandboxClient

client = SandboxClient()

sandbox = client.create_sandbox(
    warmpool="python-sandbox-pool",
    namespace="default",
)
try:
    sandbox.files.write(
        "hello.py",
        'print("Hello, World! Greetings from inside the sandbox.")\n',
    )
    result = sandbox.commands.run("python3 hello.py")
    print(result.stdout)
finally:
    sandbox.terminate()
```

Expected output: `Hello, World! Greetings from inside the sandbox.`

### 3. Build the custom image (jarvis's own toolchain)

Reuses the real jarvis-sandbox image as base — no dependency re-install,
just adds one file. Build context is the `jarvis-sandbox` repo root.

```bash
cd ~/workspace/jarvis-all/jarvis-sandbox

# Find the currently-deployed jarvis-sandbox tag to build on top of:
kubectl get deployment sandbox -n jarvis -o jsonpath='{.spec.template.spec.containers[0].image}'

# --provenance=false is required — see "Gotchas" below.
docker build -f Dockerfile.agentsandbox --provenance=false \
  --build-arg BASE_IMAGE=localhost:5050/root/jarvis-sandbox:<current-tag> \
  -t localhost:5050/root/jarvis-sandbox:agentsandbox-test .
```

#### Get the image into the cluster — skip the registry, use `minikube image load`

Pushing to the local GitLab registry under this project hit an
unreproducible `access forbidden` error on pull (see "Gotchas"). Workaround
that sidesteps the registry entirely:

```bash
# The image needs the SAME name the SandboxTemplate references (see below) —
# `localhost:5050/...` and `host.minikube.internal:5050/...` are two
# different image identities to Docker even though it's the same registry.
docker tag localhost:5050/root/jarvis-sandbox:agentsandbox-test \
  host.minikube.internal:5050/root/jarvis-sandbox:agentsandbox-test

minikube image load host.minikube.internal:5050/root/jarvis-sandbox:agentsandbox-test
```

Re-run this (build + tag + `minikube image load`, then delete the running
pod so it restarts on the fresh image) every time `agentsandbox_server.py`
changes — the tag doesn't change so Kubernetes won't know to pull a new
version on its own. (Once real CI pushes this — Phase 2 — this workaround
goes away; only ad-hoc local `docker push` ever hit the registry bug.)

### 4. Create the Template + WarmPool

```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxTemplate
metadata:
  name: jarvis-agentsandbox-template
  namespace: default
spec:
  podTemplate:
    spec:
      containers:
      - name: simple-sandbox
        image: host.minikube.internal:5050/root/jarvis-sandbox:agentsandbox-test
        ports:
        - containerPort: 8888
        readinessProbe:
          httpGet: {path: "/", port: 8888}
          initialDelaySeconds: 0
          periodSeconds: 1
        livenessProbe:
          httpGet: {path: "/", port: 8888}
          initialDelaySeconds: 2
          periodSeconds: 10
        resources:
          requests: {cpu: "250m", memory: "512Mi", ephemeral-storage: "512Mi"}
      restartPolicy: "OnFailure"
---
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxWarmPool
metadata:
  name: jarvis-agentsandbox-pool
  namespace: default
spec:
  replicas: 1
  sandboxTemplateRef:
    name: jarvis-agentsandbox-template
EOF

kubectl get pods -n default -w   # wait for 1/1 Running
```

`SandboxWarmPool` doesn't reference an image directly — it only points at
a `SandboxTemplate` by name; the image lives inside that Template's
`podTemplate.spec.containers[0].image`.

### 5. Run the jarvis-image test

`k8s-agent-sandbox` is already installed from step 2.

```python
# save as test_heavy.py, then: python3 test_heavy.py
from k8s_agent_sandbox import SandboxClient

client = SandboxClient()
sandbox = client.create_sandbox(
    warmpool="jarvis-agentsandbox-pool",
    namespace="default",
)
try:
    result = sandbox.commands.run(
        "python3 -c \"import pandas as pd; print(pd.DataFrame({'a':[1,2,3]}).sum())\""
    )
    print(result)
finally:
    sandbox.terminate()
```

Expected output: `stdout='a    6\ndtype: int64\n' stderr='' exit_code=0`

`SandboxClient()` defaults to Tunnel mode (`kubectl port-forward` under the
hood) — works from any machine with `kubectl` pointed at the cluster, no
public IP/domain needed. Production traffic (jarvis-backend) would instead
call `sandbox-router-svc.agent-sandbox-system.svc.cluster.local` directly
over the cluster network — see Phase 2, step B.

### Gotchas hit along the way

- **`host.minikube.internal` only resolves from inside minikube.** From the
  host machine (`docker build`/`docker push`), use `localhost:5050`
  instead. Every Jenkinsfile in this workspace already does this
  (`IMAGE = "localhost:5050/root/..."`)  — same registry, different
  hostname depending on which side you're calling from.
- **GitLab registry: pushing a brand-new top-level repo name gets denied.**
  GitLab Container Registry repos are tied to GitLab *projects*; pushing to
  `root/jarvis-sandbox-agentsandbox` (a project that doesn't exist) fails
  with "repository does not exist". Fix: push as a sub-path of an existing
  project instead — `root/jarvis-sandbox:<new-tag>` (same repo, new tag) —
  not `root/jarvis-sandbox/some-new-name`.
- **Docker's default `--provenance` attestation broke the push.** Recent
  Docker (buildx/containerd image store) attaches a provenance attestation
  manifest by default; this registry's cross-repo blob mount didn't handle
  it, failing with `blob unknown to registry`. Fix: always build with
  `--provenance=false` for this registry.
- **`access forbidden` pulling a manually-pushed tag — never resolved,
  worked around instead.** After push succeeded (`docker push` reported
  `Pushed`/digest), the SAME tag failed to pull from the cluster with
  `denied: access forbidden`. Verified extensively that this wasn't a
  credentials/permissions problem: manually replayed the exact JWT
  auth → manifest fetch → blob fetch sequence from inside the minikube node
  using the node's own stored credentials, and every step returned `200
  OK`. Kubelet's own pull attempt still failed. Root cause not identified
  (likely a registry-side quirk specific to this instance/version) — the
  practical fix is `minikube image load` (see step 3), which bypasses the
  registry pull path entirely.

  **Confirmed 2026-09-13: this also hits a real Jenkins-built push, not just
  ad-hoc local `docker push`.** Pushing `jarvis-sandbox` commit `33767e2`
  triggered the normal Jenkins job (`docker build` the default `Dockerfile`,
  `docker push`, ArgoCD bumps the `sandbox` Deployment's image to
  `host.minikube.internal:5050/root/jarvis-sandbox:33767e20`) and the new pod
  sat in `ImagePullBackOff` → `ErrImagePull` with the exact same `denied:
  access forbidden` on the manifest HEAD request. In the same batch,
  jarvis-backend's Jenkins-built push (`d26a1b9`) pulled and rolled out
  clean — so this isn't a blanket "Jenkins vs. manual" split as originally
  guessed, it looks tag/image-specific (maybe repo-path- or size-specific)
  and still needs a real root cause. Until then, **assume every Phase 2 E
  Jenkins push of a `jarvis-sandbox` image can hit this** and be ready to
  apply the same recovery on the real production Deployment:

  ```bash
  # <tag> = the tag ArgoCD set on the stuck deployment/sandbox image
  docker pull localhost:5050/root/jarvis-sandbox:<tag>
  docker tag localhost:5050/root/jarvis-sandbox:<tag> \
    host.minikube.internal:5050/root/jarvis-sandbox:<tag>
  minikube image load host.minikube.internal:5050/root/jarvis-sandbox:<tag>
  kubectl delete pod -n jarvis -l app=sandbox   # picks up the now-cached image
  kubectl rollout status deployment/sandbox -n jarvis
  ```

  This same incident is also why the two image identities note above
  matters in practice, not just in theory: the first `minikube image load`
  attempt during this recovery used `localhost:5050/...` (matching what
  `docker pull` had just fetched) and silently succeeded but didn't fix
  anything — the Deployment references `host.minikube.internal:5050/...`,
  a different image identity to containerd even at the same digest. Re-tag
  before loading, every time.
- **The docs site doesn't match the installed SDK.** The "Custom
  Environment" doc page shows a server expecting
  `{"command": {"content": ..., "env": ...}}` in and `{"exitCode": ...}`
  out. The actually-installed `k8s-agent-sandbox` pip package's
  `CommandExecutor.run()` (read directly from
  `.venv/lib/.../k8s_agent_sandbox/commands/command_executor.py`) sends a
  plain `{"command": "<string>"}` and parses the response via
  `ExecutionResult(stdout, stderr, exit_code)` — snake_case, no `env`
  support in the plain-HTTP (non-`sandboxd`) path at all.
  `agentsandbox_server.py` here matches the *installed SDK*, not the docs.
  If a newer SDK version changes this again, re-check by reading
  `command_executor.py` and `models.py` directly rather than trusting the
  docs site — same applies to whatever jarvis-backend ends up calling
  directly in Phase 2 (don't trust the docs' request/response shape without
  re-verifying against installed code or a live curl test).

### Tear down Phase 1's test resources

```bash
kubectl delete sandboxwarmpool jarvis-agentsandbox-pool python-sandbox-pool -n default --ignore-not-found
kubectl delete sandboxtemplate jarvis-agentsandbox-template python-sandbox-template test-sandbox-template -n default --ignore-not-found
kubectl get pods -n default -w   # wait for agent-sandbox pods to disappear, Ctrl+C

kubectl delete namespace agent-sandbox-system

VERSION=$(curl -s https://api.github.com/repos/kubernetes-sigs/agent-sandbox/releases/latest | jq -r '.tag_name')
kubectl delete -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/extensions.yaml
kubectl delete -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/sandbox.yaml

kubectl get crd | grep agents.x-k8s.io   # should be empty
kubectl get ns | grep agent-sandbox      # should be empty
```

Only do this if abandoning the migration — Phase 2 needs the install kept
(and hardened, see below) rather than torn down.

---

## Phase 2 — production migration (A/B/C shipped to prod repos, D/E/F remaining)

Concrete plan, in dependency order. Each step is independently verifiable
— A, C, and the core of B have now actually been verified live (not just
planned), each turned up a real bug that pure reading wouldn't have
caught. Still nothing is wired into jarvis-backend or deployed — that's F,
and it's the one step this pass deliberately did *not* do.

### A. Harden `agentsandbox_server.py` + the pod spec — **done, verified live**

Ported `podspec.py`'s pod- and container-level `securityContext` plus the
three `emptyDir` volume mounts (`/workspace`, `/tmp`, `/var/tmp`) into the
`SandboxTemplate`. The working YAML:

```yaml
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxTemplate
metadata:
  name: jarvis-agentsandbox-template
  namespace: default
spec:
  podTemplate:
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        fsGroup: 1000
        seccompProfile: {type: RuntimeDefault}
      containers:
      - name: simple-sandbox
        image: host.minikube.internal:5050/root/jarvis-sandbox:<tag>
        ports: [{containerPort: 8888}]
        readinessProbe: {httpGet: {path: "/", port: 8888}, initialDelaySeconds: 0, periodSeconds: 1}
        livenessProbe: {httpGet: {path: "/", port: 8888}, initialDelaySeconds: 2, periodSeconds: 10}
        securityContext:
          allowPrivilegeEscalation: false
          capabilities: {drop: ["ALL"]}
          readOnlyRootFilesystem: true
        resources:
          requests: {cpu: "100m", memory: "256Mi", ephemeral-storage: "512Mi"}
          limits: {cpu: "2", memory: "2Gi"}
        volumeMounts:
        - {name: workspace, mountPath: /workspace}
        - {name: tmp, mountPath: /tmp}
        - {name: vartmp, mountPath: /var/tmp}
      restartPolicy: "OnFailure"
      volumes:
      - {name: workspace, emptyDir: {sizeLimit: 2Gi}}
      - {name: tmp, emptyDir: {sizeLimit: 1Gi}}
      - {name: vartmp, emptyDir: {sizeLimit: 256Mi}}
```

**NetworkPolicy is not written yet** — jarvis-sandbox's own DNS-only
egress rule still needs porting over, selecting whatever labels the
controller stamps (`agents.x-k8s.io/created-by=controller`,
`agents.x-k8s.io/sandbox-template-ref-hash=...`, etc. — confirmed via
`kubectl get pod <name> --show-labels`, not assumed). Do this before
anything here is production-reachable — nothing here today restricts
egress at all.

**Two real bugs found getting this working, both worth knowing about:**

1. **`kubectl patch --type merge` silently wiped the whole hardened
   config.** Patching just the image field —
   `kubectl patch sandboxtemplate ... -p '{"spec":{"podTemplate":{"spec":{"containers":[{"name":...,"image":...}]}}}}'`
   — replaced the *entire* `containers` array (JSON Merge Patch/RFC 7386
   replaces arrays wholesale, it doesn't merge elements), silently
   dropping `volumeMounts`/`resources`/`securityContext`/`ports`/probes
   that had been applied moments earlier. Symptom: `/workspace` fell back
   to the image's own baked-in directory (`root:root`, mode `755`, old
   timestamp) instead of the mounted `emptyDir` — every write failed with
   `PermissionError`. **Always `kubectl delete` + re-`apply` the full YAML
   when changing an image tag on this template — never `kubectl patch` a
   `containers[]` field.**
2. **`minikube image load` silently no-ops when the tag already exists**
   — rebuilding the image and reloading under the *same* tag did not
   actually update what the pod ran; `kubectl exec ... cat
   agentsandbox_server.py` inside the "just-updated" pod still showed the
   old code. Confirmed via image digest comparison. Fix: use a fresh
   unique tag every rebuild (`agentsandbox-$(date +%s)`), or
   `minikube image rm` the old tag first (needs `--force`-equivalent if a
   container still references it — simplest to just avoid tag reuse
   entirely).
3. **Recreating the `Pod` alone doesn't pick up a `SandboxTemplate`
   change** — `kubectl delete pod <name>` gets a fresh pod back, but from
   the *same already-resolved* `Sandbox`/`SandboxWarmPool` spec, not a
   re-read of the current `SandboxTemplate`. Delete and recreate the
   `SandboxWarmPool` itself to force it to re-read the template.

### B. Adapt `jarvis-backend`'s sandbox client — **code written, core logic verified live, not wired in**

New file: `jarvis-backend/app/agents/tools/sandbox_manager_agentsandbox.py`
— same public surface as `sandbox_manager.py` (`exec_bash`, `read_file`,
`reset`, `get_thread_id`, `normalize_workspace_path`) so it's a drop-in
candidate, not used by any tool yet.

**Design, settled after live testing ruled out the alternative:**

- **`SandboxInClusterConnectionConfig`, not Tunnel mode or hand-rolled
  router calls.** Tunnel mode shells out to `kubectl port-forward` per
  sandbox — wrong for a long-running backend (extra process per sandbox,
  needs `kubectl` + kubeconfig baked into the image). The obvious
  alternative — call `sandbox-router-svc` directly with
  `X-Sandbox-ID`/`X-Sandbox-Namespace` headers, no SDK — was tried and
  **empirically fails** for a sandbox not claimed through the SDK: curled
  the router directly for a manually-created warm pod and got `502
  Could not connect to the backend sandbox`; router logs showed it tried
  `<name>.default.svc.cluster.local` (agent-sandbox creates no
  per-Sandbox Service, so this never resolves) — `Name or service not
  known`. The exact same router *did* successfully proxy straight to a
  pod IP for a sandbox that had gone through `create_sandbox()`. So the
  router's routing table is populated by the claim lifecycle, not by a
  Sandbox object merely existing — which means claiming has to go through
  the real client one way or another, and once it does,
  `SandboxInClusterConnectionConfig` (client resolves the pod IP itself
  from the Sandbox's own status, no router hop) is the natural choice,
  not a shortcut around anything.
- **A Kubernetes label carries `thread_id → claim_name`, not a database.**
  `create_sandbox()` always mints its own random `sandbox-claim-<uuid8>`
  name (not overridable), so a later `bash` call can't just recompute the
  claim name from `thread_id`. Instead the claim is labeled
  `jarvis-thread=<thread_id>` at creation; a later call does
  `list_all_sandboxes(label_selector="jarvis-thread=<thread_id>")` then
  `get_sandbox(...)` to reattach. **Verified live, standalone** (not
  through jarvis-backend, just the SDK directly): created a labeled
  sandbox, ran a command, looked it up again by label, reattached, ran a
  second command — both hit the exact same pod hostname. This was the
  one piece of the design with no precedent in the docs or examples.
- **Command timeouts raise, they don't come back as a graceful field.**
  jarvis-sandbox's own agent catches its own timeout and returns
  `{"timed_out": true, ...}`; `k8s_agent_sandbox`'s
  `CommandExecutor.run(command, timeout)` has no such thing — `timeout`
  is just the HTTP client's request timeout, and hitting it raises
  `SandboxRequestError`. `sandbox_manager_agentsandbox.py` catches this
  and translates it back into the `{"timed_out": True, ...}` shape
  `bash.py` already expects, so nothing above this module needs to
  change.

**What's still unverified, and can't be verified from outside the
cluster:** `SandboxInClusterConnectionConfig` specifically requires
running *inside* the cluster (resolves the pod's IP and connects
directly — no port-forward to fall back on from a laptop). Every test
above used `SandboxClient` (sync, Tunnel mode) or the async client's
*claim-management* calls, which work identically regardless of
connection mode — but the actual in-cluster HTTP path has not been
exercised. That needs either a throwaway pod inside the cluster running
this module, or wiring it into jarvis-backend for real (which also needs:
a new RBAC Role/RoleBinding for jarvis-backend's ServiceAccount —
`get`/`list`/`watch`/`create`/`delete` on `sandboxclaims` and
`get`/`list`/`watch` on `sandboxes`, both in `extensions.agents.x-k8s.io`
— it has none today, only talks HTTP to the orchestrator Service; and a
NetworkPolicy allowing jarvis-backend's pod → Sandbox pods on 8888,
since InCluster mode bypasses the router entirely).

**Known gap:** `read_file()`'s mime-type reporting falls back to
guessing from the extension client-side — `agentsandbox_server.py`'s
`/download` response doesn't echo a filename the way
`sandbox_manager.py`'s does today. Fine for `present_file`'s current call
sites (they already have the filename), worth revisiting if that changes.
Also: the download-of-a-directory (400) case through this path hasn't
been exercised, only the 404 (missing file) and success cases.

### C. Test the Filesystem API against real deliverables — **done, verified live**

`agentsandbox_server.py` didn't have `/upload`/`/download/<path>` at all
until this pass — Phase 1 only exercised the stock `python-sandbox`
image's own built-in file endpoints, not the jarvis custom image's. Added
both, mirroring the SDK's actual (non-sandboxd) wire format read straight
from `k8s_agent_sandbox/files/filesystem.py`: `POST /upload` as
multipart-form (field `file`, filename = destination path), `GET
/download/<path>` for raw bytes back. Backed by two new `runner.py`
functions (`write_file`, and a `_resolve_in_workspace` helper factored
out of the existing `read_file` so both share the same path-safety
checks).

Verified against the real jarvis image, not just plain text:
- Generated an actual `.docx` via `python-docx` inside the sandbox,
  downloaded it back through the new endpoint: 36,647 bytes, correct
  `PK\x03\x04` zip magic bytes.
- Uploaded and re-downloaded an arbitrary binary payload (UTF-8
  multi-byte characters included): byte-exact round-trip.

**One more real bug found along the way:** `/upload` 500'd with
`AssertionError: The 'python-multipart' library must be installed to use
form parsing` — FastAPI's `request.form()` needs it, and it wasn't in
jarvis-sandbox's `requirements.txt` (only needed for this new endpoint,
not the existing `/sandbox/exec|read|reset` protocol). Added
`python-multipart==0.0.12` to `requirements.txt`. Rebuilding the full
base image after this addition was fast — Docker's layer cache kept every
`pip install numpy/pandas/...` layer intact, only the small
`requirements.txt` install layer and everything after it re-ran.

### D. Benchmark warm-pool behavior at jarvis's actual scale — not started

jarvis-sandbox today: on-demand only (no idle warm pool,
`POOL_SIZE=0`), hard ceiling `MAX_SANDBOXES=6`, per-pod TTL
(`SANDBOX_TTL_MINUTES=180`). `SandboxWarmPool` is a genuinely different
model — pre-warmed replicas sitting ready. Before committing:
- Cold-start latency: on-demand agent-sandbox pod vs. jarvis-sandbox's
  current cold pod creation, same image, same node.
- Resource cost of keeping N pods warm at all times (jarvis-sandbox
  deliberately doesn't, per an earlier explicit decision — see
  [[jarvis-sandbox-service]]) vs. the latency win.
- Whether `SandboxWarmPool` supports (or can be made to support) the same
  "6 as a hard ceiling, not a target" + per-pod TTL behavior, or whether
  that logic has to move into jarvis-backend/a controller of its own.

### E. GitOps placement — not started

Once the Template/WarmPool YAML is final, it needs to live in
`jarvis-deploy` (not ad-hoc `kubectl apply`, and not this repo) the same
way `sandbox/base/*.yaml` does today, with its own ArgoCD Application —
mirror the pattern `argocd/sandbox-application.yaml` already uses. The
image build/push (`Dockerfile.agentsandbox`) needs a real Jenkins job the
same as jarvis-sandbox's own (see the Jenkinsfile in this repo). The
registry `access forbidden` gotcha above is **confirmed to reproduce through
Jenkins' push path** (hit it on the real `33767e2` production push, not just
ad-hoc local pushes — see Phase 1 Gotchas) — any Jenkins job for this image
needs either a real fix for the registry issue first, or the `minikube
image load` recovery wired in as an automated post-deploy step, not left as
something a human has to remember to do by hand.

### F. Cutover — not started, deliberately

This pass stopped here on purpose: everything above is local/reviewable
code plus test resources in a namespace nothing production depends on.
Cutover touches jarvis-backend's real deployment and (at the end)
decommissions jarvis-sandbox's real orchestrator — a live-traffic,
hard-to-reverse change, not something to do as a side effect of a
research pass. Whoever picks this up:

Only after A–E are each independently verified: point
`sandbox_manager.py` at the new path behind a flag, run both stacks side
by side for a real conversation sample, compare correctness first,
latency/cost second, then decommission the jarvis-sandbox orchestrator
Deployment and its CRD-free pod-pool code once nothing depends on it.

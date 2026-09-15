# Migrating jarvis-sandbox to kubernetes-sigs/agent-sandbox

**Status: done.** As of 2026-09-14, real jarvis traffic runs entirely on
[kubernetes-sigs/agent-sandbox](https://agent-sandbox.sigs.k8s.io/docs/).
jarvis-sandbox's own orchestrator (the Deployment that used to create/track/
delete agent pods itself) has been decommissioned — deleted from the
cluster, its GitOps manifests removed, and its code deleted from this repo.
`jarvis-backend/app/agents/tools/sandbox_manager.py` talks to agent-sandbox's
controller directly; there is no flag, no fallback, no dispatcher — this
*is* the sandbox backend now.

This doc is kept as the history of how that happened — Phase 1 (install +
sanity-check agent-sandbox itself), Phase 2 A–G (harden it, adapt
jarvis-backend, wire it in, benchmark it, GitOps it, cut traffic over, tear
the old thing down, then work out what the router can and cannot do about
one conversation reaching another's sandbox).
Read top to bottom for the story, or jump to
[Phase 2 step F](#f-cutover--done-2026-09-14-jarvis-sandboxs-own-orchestrator-no-longer-exists)
for what the actual cutover looked like and what got deleted, or
[step G](#g-cross-conversation-access--what-was-found-what-the-router-can-and-cant-do-2026-09-15)
for a known, user-accepted gap: one sandbox pod can still reach another's
IP directly, and **no router configuration affects that** — it is traffic
the router never sees. Normal per-conversation usage is isolated and
verified; the exposure needs adversarial code running inside a sandbox.
Step G is also where several confident-but-wrong conclusions got corrected,
so read it before trusting anything earlier in this doc about router auth.
For the practical "how is this actually deployed, what do I run to redo
it", skip straight to [Current deployment](#current-deployment) below.

> [DEPLOY-STANDALONE.md](DEPLOY-STANDALONE.md) is now **stale** — it was
> written before this cutover for a different audience (deploying
> jarvis-sandbox's own orchestrator behind a *different* chatbot) and still
> frames that orchestrator as "the current system". Not rewritten yet;
> until it is, this doc is the accurate one.

## Current deployment

The practical version — no narrative, just what's actually running and
what to run to reproduce it. Two halves: the sandbox side (this repo +
agent-sandbox's CRDs) and the jarvis-backend side (the client code that
calls it).

### Prerequisites (once per cluster)

agent-sandbox's controller, CRDs, and router — see [Phase 1 §1](#1-install)
for the exact install commands. Confirm they're there before anything
below will work:

```bash
kubectl get pods -n agent-sandbox-system   # agent-sandbox-controller + sandbox-router-deployment, both Running
kubectl get crd | grep agents.x-k8s.io     # sandboxes / sandboxclaims / sandboxtemplates / sandboxwarmpools
```

### Sandbox side — build the image, apply the Template/WarmPool

```bash
cd jarvis-sandbox
# base image first (the real toolchain — pandas/docx/pandoc/...)
docker build --provenance=false -t <registry>/jarvis-sandbox:<tag> .
docker push <registry>/jarvis-sandbox:<tag>
# thin adapter layer on top — swaps the entrypoint to agentsandbox_server.py
docker build --provenance=false -f Dockerfile.agentsandbox \
  --build-arg BASE_IMAGE=<registry>/jarvis-sandbox:<tag> \
  -t <registry>/jarvis-sandbox:agentsandbox-<tag> .
docker push <registry>/jarvis-sandbox:agentsandbox-<tag>
```

Then point the GitOps-tracked Template at that tag and push:

```bash
cd jarvis-deploy
# edit agentsandbox/overlays/test/kustomization.yaml's images: newTag
git add agentsandbox/overlays/test/kustomization.yaml
git commit -m "agentsandbox: bump image to <tag>"
git push
```

`jarvis-deploy/argocd/agentsandbox-application.yaml` (Application
`jarvis-agentsandbox`, `prune: true` + `selfHeal: true`) auto-syncs
`agentsandbox/overlays/test` from there — no manual `kubectl apply` needed
once that Application already exists. First-time setup on a cluster that's
never had it: `kubectl apply -f jarvis-deploy/argocd/agentsandbox-application.yaml`
once, registering it; every push after that syncs on its own.

The live Template today (verified straight off the cluster, not
transcribed from memory):

```yaml
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxTemplate
metadata:
  name: jarvis-agentsandbox-template
  namespace: default
spec:
  podTemplate:
    metadata:
      labels: {app: jarvis-agentsandbox-agent}
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        fsGroup: 1000
        seccompProfile: {type: RuntimeDefault}
      containers:
      - name: simple-sandbox
        image: host.minikube.internal:5050/root/jarvis-sandbox:agentsandbox-1789297905
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
---
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxWarmPool
metadata:
  name: jarvis-agentsandbox-pool
  namespace: default
spec:
  replicas: 1   # ~30ms warm claim vs ~1.5s cold — see Phase 2 step D
  sandboxTemplateRef:
    name: jarvis-agentsandbox-template
```

One thing the controller adds on its own that isn't in the YAML above:
`kubectl get sandboxtemplate ... -o yaml` also shows
`networkPolicyManagement: Managed` — the controller auto-creates its own
per-Template NetworkPolicy (`jarvis-agentsandbox-template-network-policy`,
selector on the auto-stamped `sandbox-template-ref-hash` label). The
`jarvis-agentsandbox-agent` NetworkPolicy in
`jarvis-deploy/agentsandbox/base/networkpolicy.yaml` was written by hand
before noticing this — the two overlap; worth checking whether the
hand-written one is still pulling weight or the controller's own makes it
redundant, not yet done.

### jarvis-backend side — RBAC, ServiceAccount, code

**RBAC** (manual `kubectl apply`, deliberately not GitOps'd — see Phase 2
step E for why):

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: jarvis-backend
  namespace: jarvis
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: jarvis-backend-agentsandbox
  namespace: default
rules:
  - apiGroups: ["extensions.agents.x-k8s.io"]
    resources: ["sandboxclaims"]
    verbs: ["get", "list", "watch", "create", "delete"]
  - apiGroups: ["agents.x-k8s.io"]   # core CRD — NOT extensions.*, easy to get wrong (see Phase 2 step F)
    resources: ["sandboxes"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: jarvis-backend-agentsandbox
  namespace: default
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: Role, name: jarvis-backend-agentsandbox}
subjects:
  - {kind: ServiceAccount, name: jarvis-backend, namespace: jarvis}
```

This RBAC (unchanged since Phase 2 step F) covers jarvis-backend's direct
k8s-API calls: create/list/get/delete on `sandboxclaims`, read on
`sandboxes`. The `sandboxes` grant is load-bearing in a way that isn't
obvious and shouldn't be trimmed as dead weight — the SDK reads
`.status.podIPs` from the Sandbox to tell the router which pod to forward
to (`X-Sandbox-Pod-IP`). Remove it and routing breaks, not just status
reporting.

The router needs no RBAC and no ServiceAccount of its own: it never calls
the Kubernetes API, since the client hands it the pod IP. (An earlier
version of this setup gave it both — see step G for why that turned out to
be unnecessary.)

**ServiceAccount wiring** — `jarvis-deploy/backend/base/backend.yaml`'s
`template.spec.serviceAccountName: jarvis-backend` (GitOps'd like any other
Deployment field — no reason this one needs to be manual).

**Code** — `jarvis-backend/app/agents/tools/sandbox_manager.py` is the
entire client, no flag/dispatcher (removed once the old orchestrator was
gone — see Phase 2 step F). It goes **through the router**, not straight to
a pod IP; see [step G](#g-cross-conversation-access--what-was-found-what-the-router-can-and-cant-do-2026-09-15)
for why pod-direct is the thing to avoid. The shape any call site uses:

```python
# app/agents/tools/sandbox_manager.py
from k8s_agent_sandbox import AsyncSandboxClient
from k8s_agent_sandbox.models import SandboxDirectConnectionConfig

_ROUTER_URL = "http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080"

def init_client() -> None:
    global _client
    _client = AsyncSandboxClient(
        connection_config=SandboxDirectConnectionConfig(api_url=_ROUTER_URL, server_port=8888),
    )

async def _get_or_create_sandbox(thread_id: str):
    ...  # claims by thread_id label, reuses if it exists
    # No credential is sent: the router runs ALLOW_UNAUTHENTICATED_ROUTER="true".
    # To switch that off, inject the shared ROUTER_AUTH_TOKEN here —
    # SandboxDirectConnectionConfig has no headers field, but
    # sandbox.connector.client is a real, shared, settable httpx.AsyncClient.
    return sandbox

async def exec_bash(thread_id: str, command: str) -> dict:
    """Returns {stdout, stderr, exit_code, timed_out}."""
    sandbox = await _get_or_create_sandbox(thread_id)
    # generic templates run commands via shlex.split()+subprocess, not a
    # real shell — wrap so &&/|/>/heredocs work regardless of backend image
    wrapped = "bash -c " + shlex.quote(command)
    result = await sandbox.commands.run(wrapped, timeout=300)
    ...

async def read_file(thread_id: str, name: str) -> tuple[bytes, str, str]: ...
async def reset(thread_id: str) -> None: ...   # deletes the claim
```

`bash.py`, `present_file.py`, `chat.py`'s `/sandbox-file` endpoint, and
everything else import `exec_bash`/`read_file`/`reset`/`get_thread_id`
from here exactly like they always did — the whole point of keeping the
same public surface across the migration was that none of them ever
needed to change. One thing that *did* need fixing post-cutover: those
call sites originally caught `httpx.HTTPError`/`httpx.HTTPStatusError`
around `read_file()` (left over from the old httpx-based orchestrator
client) — the SDK actually raises `k8s_agent_sandbox.exceptions.SandboxRequestError`.
Fixed 2026-09-15 in `present_file.py` and `chat.py`; see that commit if
adding a new call site that reads files.

**Config** (`app/core/config.py`):

```python
AGENTSANDBOX_NAMESPACE: str = "default"
AGENTSANDBOX_WARMPOOL: str = "python-sandbox-pool"
```

`AGENTSANDBOX_WARMPOOL` points at agent-sandbox's own stock generic
template (`python-sandbox-pool`, backed by `python-runtime-sandbox`), not
jarvis's own `Dockerfile.agentsandbox`-built image/pool
(`jarvis-agentsandbox-pool`, from Phase 2 step D) — a deliberate choice
made mid-session to compare the two; both templates work through this
client, switching back is just this one ConfigMap key in
`jarvis-deploy/backend/base/configmap.yaml`.

**Dependency** (`requirements.txt`): `k8s-agent-sandbox[async]==1.0.2` —
hard requirement now, not conditional on anything.

### Verify

Same smoke test used throughout this migration — from inside a real
`backend` pod, through the actual `bash` tool (not a hand-rolled request):

```bash
kubectl exec -n jarvis deploy/backend -- python3 -c "
import asyncio, sys
sys.path.insert(0, '/app')

async def main():
    from app.agents.tools.bash import bash
    from app.agents.tools import sandbox_manager
    sandbox_manager.init_client()
    thread_id = 'verify-test'
    config = {'configurable': {'thread_id': thread_id}}
    try:
        result = await bash.ainvoke(
            {'command': 'echo ok && python3 -c \"print(6*7)\"', 'label': 'verify'},
            config=config,
        )
        print(result)
    finally:
        await sandbox_manager.reset(thread_id)
        await sandbox_manager.close_client()

asyncio.run(main())
"
```

Expect `ok` then `42`, no traceback.

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
# The second sed flips ALLOW_UNAUTHENTICATED_ROUTER to "true" (upstream's
# file ships "false"). This is still what's deployed — see step G for the
# trade-off and why it doesn't affect per-conversation isolation.
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
- **The deployed `sandbox-router:latest-main` is a stale, functionally
  different build from the `v1.0.2` source tag.** Confirmed 2026-09-14:
  routing a genuinely SDK-claimed sandbox through the router (not a
  guessed pod name — a real `create_sandbox()` claim) still 502'd. The
  router's own pod logs gave the real reason —
  `ERROR: Connection to sandbox at http://<pod>.default.svc.cluster.local:8888/...
  failed. Error: [Errno -2] Name or service not known` — a **Python**
  `socket.gaierror` message format, not Go. The `v1.0.2` source
  (`sandbox-router/cache/cache.go`) is an informer-backed Go rewrite that
  indexes claimed pods by name specifically so this resolves; whatever
  commit `latest-main` was built from predates that rewrite entirely and
  always falls to the broken DNS form. Since `latest-main` is a
  perpetually-rebuilt staging tag (see cloudbuild.yaml: triggered off the
  `main` branch, no version pin, no changelog to check against), there's
  no way to know when/whether it'll pick up the Go rewrite without
  rebuilding it yourself from a real tag — reinforcing why "build the
  router from the `v1.0.2` source tag and stop depending on
  `us-central1-docker.pkg.dev`'s staging build" (source already cloned,
  Dockerfile + build command ready — see the migration doc's own repo
  history / ask whoever has it) isn't just a company-network workaround,
  it's the only way router-mode connections work *at all* against a
  version-matched agent-sandbox install. Until that's built and deployed,
  anything using this SDK from outside the cluster (or any connection mode
  other than `SandboxInClusterConnectionConfig`) will 502 against this
  cluster's router, regardless of claim status.

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

## Phase 2 — production migration (A–F all done — this is complete)

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

- **`SandboxInClusterConnectionConfig`, not Tunnel mode or the router.**
  Tunnel mode shells out to `kubectl port-forward` per sandbox — wrong for
  a long-running backend (extra process per sandbox, needs `kubectl` +
  kubeconfig baked into the image). Routing through `sandbox-router-svc`
  instead (`SandboxDirectConnectionConfig`, the SDK's real router-mode
  class — auto-injects `X-Sandbox-ID`/`-Namespace`/`-Port`, no need to
  hand-roll headers) was tried **twice**, both times against a sandbox
  genuinely claimed via `create_sandbox()`, and **both times 502'd**:
  `{"detail":"Could not connect to the backend sandbox: <pod>"}`. (An
  earlier note here claimed the router *did* work for a claimed sandbox —
  that was wrong; re-verified 2026-09-14 with router pod logs checked
  directly, not inferred, and it fails every time against what's actually
  deployed.) Root cause, from the router's own logs: `Proxying request for
  sandbox '<pod>' to URL: http://<pod>.default.svc.cluster.local:8888/...`
  then `ERROR: ... Name or service not known` — the deployed
  `sandbox-router:latest-main` always falls to DNS-form resolution
  (`<id>.<namespace>.svc.cluster.local`, which never resolves — no
  per-Sandbox Service exists), because it turns out to be an **old Python
  build with no pod-IP cache at all** — not the informer-cache-backed Go
  rewrite that exists at the `v1.0.2` source tag (see the router-image
  Gotcha below; `latest-main` is a perpetually-rebuilt staging tag, no
  version guarantee whatsoever). So: claiming *does* have to go through
  the real client (the router genuinely can't resolve a guessed name
  either way), but the router itself can't currently complete the proxy
  regardless of claim status — making `SandboxInClusterConnectionConfig`
  (client resolves the pod IP itself from the Sandbox's own status, no
  router hop, unaffected by this bug) not just the tidier choice but
  presently the *only working one*. Revisit router mode once a
  `v1.0.2`-built router image is actually deployed — the code for that
  connection mode is trivial to swap back in (see git history on this
  file for the exact diff tried).
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
`get`/`list`/`watch`/`create`/`delete` on `sandboxclaims`
(`extensions.agents.x-k8s.io`) and `get`/`list`/`watch` on `sandboxes`
(`agents.x-k8s.io` — the **core** CRD, a different apiGroup from the
extension ones; mixing these up is an easy, silent mistake since `kubectl
apply` accepts the Role either way and only `kubectl auth can-i` catches
it — confirmed live 2026-09-14) — it has none today, only talks HTTP to
the orchestrator Service; and a
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

### D. Benchmark warm-pool behavior at jarvis's actual scale — **done, verified live**

Measured 2026-09-13 against the real hardened template/pool (jarvis's own
image), same node, same image, both paths cold where comparable:

| path | latency |
|---|---|
| jarvis-sandbox orchestrator, on-demand (`POOL_SIZE=0`, current prod config) | **~3.08–3.10s**, consistent across 3 runs |
| agent-sandbox, warm-pool claim (`replicas: 1`, a pod already sitting ready) | **~27–31ms** — ~100x faster |
| agent-sandbox, cold on-demand (pool scaled to `0`, forces a fresh pod) | **~1.54s** — still ~2x faster than jarvis-sandbox's own cold path |

Even agent-sandbox's *cold* path beats jarvis-sandbox's current cold path
by 2x — the controller's reconcile loop plus the template's aggressive
readiness probe (`periodSeconds: 1`, `initialDelaySeconds: 0`) apparently
just beats jarvis-sandbox's own orchestrator loop, not merely a warm-pool
artifact. Resource cost of `replicas: 1` is one idle pod's requests (100m
CPU / 256Mi mem) at all times — same order of magnitude as jarvis-sandbox's
own `POOL_SIZE>0` tradeoff, nothing new to reason about there.

Not benchmarked: `SandboxWarmPool`'s behavior at `MAX_SANDBOXES`-equivalent
concurrency (6 simultaneous claims) or whether it supports a hard ceiling
the way jarvis-sandbox's pool does — still open if cutover proceeds to
real scale.

### E. GitOps placement — **Template/WarmPool/NetworkPolicy done, Jenkins job still missing**

`jarvis-deploy` commit `02d4935` (not yet pushed) adds `agentsandbox/base/`
(SandboxTemplate + SandboxWarmPool + NetworkPolicy, mirroring `sandbox/`'s
own base+overlay split) and `argocd/agentsandbox-application.yaml`
(destination namespace `default`, `prune: true` + `selfHeal: true`, same
posture as `sandbox-application.yaml`). Verified `kubectl kustomize
agentsandbox/overlays/test` before committing produces byte-identical spec
to what's actually live — first sync will be a no-op, not a surprise
rollout.

**Deliberately not GitOps'd:** the RBAC (ServiceAccount + Role +
RoleBinding letting jarvis-backend talk to `sandboxclaims`/`sandboxes`).
Granting new permissions isn't something to put behind automated sync —
stays a manual, reviewed `kubectl apply`, applied once by a human, not
something ArgoCD's `selfHeal` should be able to silently re-create if
someone deletes it. The YAML lives in this doc's Phase 2 step B notes; get
it from git history / ask whoever ran it.

**Still missing:** a real Jenkins job for `Dockerfile.agentsandbox`. No
credentials to set one up from an agent session — needs a human in the
Jenkins UI (or a Jenkinsfile + job-DSL, but even that needs someone to
register the job once). Until it exists, `agentsandbox/overlays/test`'s
image tag is bumped by hand, same as `sandbox/overlays/test` was before its
own job existed (see that overlay's own comment). Confirmed (Phase 1
Gotchas): the registry `access forbidden` bug reproduces through a real
Jenkins-built push — whoever sets this job up should expect to hit it and
have the `minikube image load` recovery ready, not be surprised by it.

### F. Cutover — **done, 2026-09-14. jarvis-sandbox's own orchestrator no longer exists.**

What actually happened, in order:

1. RBAC applied (`ServiceAccount jarvis-backend` in `jarvis`, `Role`/
   `RoleBinding jarvis-backend-agentsandbox` in `default`) — hit a real bug
   doing this: both rules were written under `extensions.agents.x-k8s.io`,
   but `sandboxes` is the **core** CRD (`agents.x-k8s.io`), not an
   extension one. `kubectl apply` accepted the wrong Role silently; only
   `kubectl auth can-i get sandboxes ...` caught the gap. Fixed live,
   documented in the Phase 2 B section above.
2. `backend` Deployment got `serviceAccountName: jarvis-backend` (via
   jarvis-deploy, not a raw `kubectl patch` — `backend`'s ArgoCD
   Application has `selfHeal: true`, which would've reverted a direct
   patch on the next sync).
3. **The one thing that mattered most, finally verified**: `exec_bash()`
   through the real dispatcher, from the real `backend` pod, with the real
   RBAC, `SANDBOX_BACKEND` overridden just for that one call
   (`kubectl exec ... env SANDBOX_BACKEND=agentsandbox python3 -c ...`) —
   created a claim, ran `python3 -c "print(6*7)" && whoami && hostname`,
   got `42` / `sandbox` / the real pod name back, clean exit.
4. `SANDBOX_BACKEND=agentsandbox` set for real in `backend-config`
   (jarvis-deploy), rolled out, confirmed live in the running pod's actual
   environment (not an override).
5. Smoke-tested the real `bash` tool itself (not just `exec_bash()`
   directly) through the naturally-configured pod — clean output, no
   manual overrides.
6. **Old orchestrator decommissioned**: `Deployment`/`Service sandbox`,
   its `ConfigMap`, its `NetworkPolicy`, its `ServiceAccount`/`Role`/
   `RoleBinding` (`jarvis-sandbox-orchestrator`) all deleted from the
   cluster; the ArgoCD Application removed first so `selfHeal` didn't
   fight the cleanup. `jarvis-deploy`'s `sandbox/overlays/test/` and
   `argocd/sandbox-application.yaml` removed from git.
   `sandbox_manager.py` collapsed back to a single direct implementation —
   no more dispatcher, no more `SANDBOX_BACKEND` flag, since there's
   nothing left to switch between. `jarvis-sandbox`'s own
   `app/orchestrator/` and the old `/api/v1/sandbox/*` protocol
   (`app/agent/app.py`) deleted too — see that repo's own commit history.

**One real production incident found and fixed along the way, unrelated to
any of the above but worth recording**: the old orchestrator had been
silently down for 13 hours (`ImagePullBackOff` on a docs-only image build
— the same registry `access forbidden` bug hit again, this time via a real
Jenkins-triggered rebuild, nobody had applied the `minikube image load`
recovery). Found and fixed while checking the rollback path was healthy
before relying on it. Nobody had noticed — a reminder that "rollback is
just flipping a flag back" is only true if the thing you'd flip back to
is actually still alive.

**Deliberately not touched**: `jarvis-deploy`'s `sandbox/base/` and
`sandbox/overlays/staging/` — still referenced by
`argocd/staging-sandbox-application.yaml`, which targets a **separate
cluster** (`destination.server: host.minikube.internal:18443`) that was
unreachable (`sync: Unknown`) at cutover time. Deleting that path would
prune that cluster's entire `jarvis` namespace next time it reconnects,
with no way to verify anything about it from here. If staging is also
moving off the old orchestrator, that's a separate decommission someone
with visibility into that environment needs to do.

### G. Cross-conversation access — what was found, what the router can and can't do (2026-09-15)

**What was found.** The old (now-deleted) orchestrator authenticated every
sandbox call with a per-pod `AGENT_TOKEN`, checked server-side by
`app/agent/app.py`'s `verify_agent_token`. That code was deleted in the
cutover (step F) along with the rest of the orchestrator, and nothing
replaced it: both `agentsandbox_server.py` (jarvis's own adapter) and
agent-sandbox's own stock `python-runtime-sandbox/main.py` accept every
request on `GET /`, `POST /execute`, `POST /upload`, `GET /download/<path>`
with **zero authentication** — by design, per upstream's own model, which
puts the security boundary at NetworkPolicy + the router, not the pod
itself. Neither of those was actually in place here: this cluster's CNI
doesn't enforce NetworkPolicy at all (confirmed repeatedly over the course
of this migration — the controller's own `networkPolicyManagement: Managed`
auto-creates per-Template NetworkPolicies that are equally inert), and
jarvis-backend was calling pods directly
(`SandboxInClusterConnectionConfig`) rather than through the router,
because the *deployed* router image
(`sandbox-router:latest-main`) was an old Python build with no pod-IP
cache and no authz framework — going through it just 502'd.

Confirmed live, from inside one conversation's sandbox pod, with no
credentials of any kind: a direct HTTP call to a second live sandbox pod's
IP on `:8888` could read that second pod's files (`GET /download/<path>`)
**and** run arbitrary commands in it (`POST /execute`). Any conversation's
sandbox could fully compromise any other conversation's sandbox, just by
knowing (or scanning for) its pod IP. This is a regression introduced by
the cutover, not a pre-existing gap in the old system.

**What normal usage was checked separately, and is fine**: two real
conversations (`thread_id` A and B), each running the actual `bash` tool
through the actual API — `ls -la` in A never sees B's files. The
compromise above requires code *inside* a sandbox pod to deliberately
reach out to another pod's IP; it does not happen as a side effect of
ordinary tool calls, and nothing in the model's own prompt/tools does
this. The vulnerability is real but requires an adversarial payload
running inside a sandbox (e.g. from a prompt injection in fetched content,
or attacker-supplied code the model executes) — not baseline exposure.

**First fix attempt, since reverted**: agent-sandbox's `sandbox-router` has
a built-in authorization framework
(`--authz-mode=allow-all|tokenreview|scoped-token`) that the cutover had
bypassed. The router was rebuilt from the `v1.0.2` Go source with
`--authz-mode=tokenreview --authz-tokenreview-require-token=true
--cache-enabled=true`, GitOps'd in `jarvis-deploy/agent-sandbox-router/`,
with hand-applied RBAC (`ServiceAccount sandbox-router` + Pod-read
`ClusterRole` + `system:auth-delegator`), and `sandbox_manager.py` switched
to `SandboxDirectConnectionConfig` against the router's Service, sending
jarvis-backend's projected ServiceAccount token as
`Authorization: Bearer <token>`. Unauthenticated calls did start getting
`401`, and the `bash` tool kept working.

**What that was actually worth — much less than it looked (2026-09-15).**
Two of the beliefs behind it were tested afterwards and both were wrong:

- *"The published router image is an old Python build with no pod-IP cache,
  so router mode 502s."* No. The 502s were misattributed. The SDK resolves
  the pod IP **itself**, from the Sandbox CR's `.status.podIPs` (see
  `async_sandbox.get_pod_ip` — it reads the *Sandbox*, so the existing
  `sandboxes: get` grant suffices; no `pods` RBAC anywhere), and passes it
  as `X-Sandbox-Pod-IP`. The router only forwards. That is why upstream's
  own quickstart YAML ships no ServiceAccount at all. Verified by running
  the published image and doing real exec + file I/O through it: clean,
  no 502.
- *"The published image has no authz at all."* No. It has
  `ALLOW_UNAUTHENTICATED_ROUTER` plus a shared `ROUTER_AUTH_TOKEN`. Run
  with the flag at upstream's own default of `"false"` and a Secret wired
  in, an unauthenticated call gets a real `401`, while authenticated calls
  work normally. Verified live.

And most importantly, **`tokenreview` never addressed cross-conversation
access in the first place.** Upstream documents the scope plainly: it
*authenticates* the caller — confirms the token belongs to some known
cluster principal — and does **not** check whether that principal may
touch the specific sandbox named in `X-Sandbox-ID`. jarvis-backend uses one
ServiceAccount token for every conversation, so there was never anything
there to tell conversations apart. The mode that does bind a credential to
a single sandbox is `scoped-token`, which requires something to mint
per-sandbox tokens at creation time (the router only verifies, never
mints) — real integration work, not a flag.

**Where it landed**: back on the published image with the quickstart's
config (`ALLOW_UNAUTHENTICATED_ROUTER="true"`), no self-built image, no
router ServiceAccount or RBAC, and `_auth_headers()` removed from
`sandbox_manager.py`. The self-built router bought nothing this deployment
needed, and cost a bespoke image to rebuild plus a `system:auth-delegator`
grant. `SandboxDirectConnectionConfig` through the router stays — that part
was right, and going pod-direct remains the thing to avoid.

**What actually provides per-conversation isolation** — and always did,
independent of every router decision above — is `_get_or_create_sandbox()`
giving each `thread_id` its own claim, hence its own pod. Verified live
through the published router with two conversations: different pods,
different `hostname`s, and B could neither list nor `cat` a file A had
written.

**What no router setting closes**, confirmed by re-running the exact same
attack while the authenticated router was live: one sandbox pod reaching
another sandbox pod's raw IP never touches the router, so nothing the
router does — TokenReview, a shared token, anything — can see it, let
alone stop it. The direct pod-to-pod compromise works unchanged under
every router configuration tried. Closing it needs one of: NetworkPolicy
enforcement at the cluster/CNI level (this minikube setup doesn't have it,
and switching CNI is disruptive well beyond this migration's scope), or
authentication on the sandbox pods' own HTTP servers (`agentsandbox_server.py`
and/or a fork of the stock template) — real work, not a config flag.

**Decision**: raised to the user 2026-09-15 with the tradeoffs above.
Explicit instruction: leave it here. Ordinary per-conversation usage is
confirmed isolated, and the residual deliberate-attack vector (a pod
reaching another pod's IP) is a known, accepted risk, not scheduled for
further work unless raised again. Scope was set explicitly at the FE → BE →
sandbox chat path.

If revisiting: NetworkPolicy is probably the smaller lift on a CNI that
supports it (Cilium/Calico, not minikube's default), since it needs no
image changes. Per-pod auth is more portable but means giving
`agentsandbox_server.py` (and possibly a fork of the stock template image)
its own token check again — effectively re-adding a scoped version of what
the old orchestrator's `verify_agent_token` did. `scoped-token` on the Go
router is the third option, and the only one that makes the *router* aware
of which sandbox a caller may touch; it needs a minting component, and it
means going back to a self-built router image.

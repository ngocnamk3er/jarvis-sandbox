# Giving a chatbot code-execution sandboxes

A from-zero guide for putting per-conversation sandboxes behind **any**
chatbot backend, not just jarvis-backend. Nothing here depends on the
`jarvis-deploy` repo or this workspace's home-lab setup — swap in your own
cluster and registry.

> **If you came here looking for jarvis-sandbox's orchestrator, it is
> gone.** This repo used to ship a Deployment that mapped
> `thread_id → pod`, created agent pods itself, and exposed
> `/api/v1/sandbox/exec` behind an `X-Internal-Api-Key` header. That was
> decommissioned on 2026-09-14 and its code deleted; the same job is now
> done by [kubernetes-sigs/agent-sandbox](https://agent-sandbox.sigs.k8s.io/docs/),
> an upstream project. See [AGENTSANDBOX-MIGRATION.md](AGENTSANDBOX-MIGRATION.md)
> for that history.

## What you actually deploy

Three upstream pieces, all from **published images — nothing to build**:

| Piece | Role |
|---|---|
| agent-sandbox **controller + CRDs** | Creates and tracks sandbox pods |
| **sandbox-router** | Proxies your backend's commands to the right pod |
| **SandboxTemplate + SandboxWarmPool** | What a sandbox pod looks like; how many stay pre-warmed |

Your backend then talks to the router using the `k8s-agent-sandbox` Python
SDK.

**What this repo still contributes is optional**: a richer runtime image
(pandas, numpy, matplotlib, python-docx, pandoc) you can swap in place of
upstream's stock Python sandbox. Skip [Step 6](#step-6--optional-a-richer-runtime-image)
and you never build anything at all.

## How isolation works

Worth understanding before you rely on it:

- **One conversation, one pod.** Your backend labels each `SandboxClaim`
  with its conversation ID and looks it up by that label. Same ID finds the
  same pod with its files intact; a new ID gets a fresh pod. This is
  something *your code* does — nothing in the cluster does it for you.
- **Sandbox pods authenticate nothing.** Anything that can reach a pod's
  IP can run commands in it. Upstream's model puts the boundary at the
  router plus NetworkPolicy, not at the pod.
- **So NetworkPolicy matters here**, and only works if your CNI enforces
  it (Calico, Cilium, most managed clusters). Without enforcement, one
  sandbox can reach another sandbox's IP directly and read or execute
  anything in it — confirmed exploitable, not theoretical. Check before
  assuming you have isolation.

## Prerequisites

- A Kubernetes cluster (1.24+), any distro, with `kubectl` and permission
  to create CRDs, Deployments, RBAC and cluster-scoped resources.
- Your chatbot backend running in that cluster (or able to reach it).
- *Strongly recommended:* a NetworkPolicy-enforcing CNI — see above.
- Docker only if you want [Step 6](#step-6--optional-a-richer-runtime-image).

## Step 1 — Controller + CRDs

```bash
VERSION=v1.0.2

kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/sandbox.yaml
kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/extensions.yaml

kubectl -n agent-sandbox-system rollout status deployment/agent-sandbox-controller --timeout=90s
```

Four CRDs land. Note the split in API groups — it matters for RBAC in
Step 4 and a wrong group applies cleanly, then fails only at runtime:

| CRD | API group |
|---|---|
| `sandboxes` | `agents.x-k8s.io` |
| `sandboxclaims`, `sandboxtemplates`, `sandboxwarmpools` | `extensions.agents.x-k8s.io` |

## Step 2 — Router

Upstream's quickstart manifest, unmodified apart from the image and one
flag:

```bash
VERSION=v1.0.2

curl -sSL https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/refs/tags/${VERSION}/clients/python/agentic-sandbox-client/sandbox-router/sandbox_router.yaml \
  | sed 's|${ROUTER_IMAGE}|us-central1-docker.pkg.dev/k8s-staging-images/agent-sandbox/sandbox-router:latest-main|g' \
  | sed '/ALLOW_UNAUTHENTICATED_ROUTER/{n;s/value: "false"/value: "true"/}' \
  | kubectl -n agent-sandbox-system apply -f -

kubectl -n agent-sandbox-system rollout status deployment/sandbox-router-deployment --timeout=90s
```

This creates `sandbox-router-deployment` and the `sandbox-router-svc`
Service your backend will call. It needs **no ServiceAccount and no RBAC** —
the router never calls the Kubernetes API, because the SDK resolves the pod
IP itself and hands it over in a header.

### About that second `sed`

It flips `ALLOW_UNAUTHENTICATED_ROUTER` from upstream's default of
`"false"` to `"true"`, so you don't have to provision a Secret. Decide
deliberately:

| Setting | Effect | Cost |
|---|---|---|
| `"true"` | Any workload that can reach the Service can address any sandbox | none |
| `"false"` | Callers with no credential get `401` | A Secret, uncommenting the `ROUTER_AUTH_TOKEN` env block, and sending `Authorization: Bearer <token>` from your backend |

**Per-conversation isolation does not depend on this flag** — that comes
from the label lookup in Step 5. What `"false"` buys is narrower than it
sounds: it rejects callers holding no token. It does *not* restrict a
caller to one specific sandbox. If you need that, the router's
`--authz-mode=scoped-token` is the only mode that binds a credential to a
single sandbox, and it requires a component that mints per-sandbox tokens
plus building the router from source.

## Step 3 — Template + warm pool

Upstream's stock runtime image. The warm pool keeps pods pre-started so a
conversation's first command doesn't pay pod startup.

```bash
kubectl apply -f - <<'EOF'
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxTemplate
metadata:
  name: python-sandbox-template
  namespace: default
spec:
  podTemplate:
    spec:
      containers:
      - name: python-runtime
        image: us-central1-docker.pkg.dev/k8s-staging-images/agent-sandbox/python-runtime-sandbox:latest-main
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
      restartPolicy: OnFailure
  volumeClaimTemplates:
  - metadata: {name: workspace}
    spec:
      accessModes: ["ReadWriteOnce"]
      resources: {requests: {storage: "1Gi"}}
  volumeClaimTemplatesPolicy: Overrides
---
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxWarmPool
metadata:
  name: python-sandbox-pool
  namespace: default
spec:
  replicas: 1
  sandboxTemplateRef:
    name: python-sandbox-template
EOF

kubectl get pods -n default -w   # wait for python-sandbox-pool-xxx → 1/1
```

Knobs, all in the Template or pool above — no image rebuild for any of
them:

- **`replicas`** on the warm pool — how many idle pods you keep. Costs
  their requests sitting idle; buys a much faster first command.
- **`resources`** on the container — per-sandbox CPU/memory ceiling.
- **`runtimeClassName: gvisor`** in `podTemplate.spec` — kernel-level
  isolation between sandboxes rather than namespace-level. Install the
  gVisor RuntimeClass on your cluster first.
- **`volumeClaimTemplates`** — drop it for ephemeral sandboxes, or raise
  the size for heavier workloads.

The controller also stamps `networkPolicyManagement: Managed` on the
Template and creates a NetworkPolicy per template. On a cluster whose CNI
doesn't enforce NetworkPolicy the object exists and does nothing — its
presence is not evidence of isolation.

## Step 4 — RBAC for your backend

Unlike the router, **your backend does need permissions** — it is the thing
calling the Kubernetes API. Replace the ServiceAccount name and namespace
with your chatbot's:

```bash
kubectl apply -f - <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: chatbot-agentsandbox
  namespace: default          # where sandboxes live
rules:
  - apiGroups: ["extensions.agents.x-k8s.io"]
    resources: ["sandboxclaims"]
    verbs: ["get", "list", "watch", "create", "delete"]
  - apiGroups: ["agents.x-k8s.io"]
    resources: ["sandboxes"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: chatbot-agentsandbox
  namespace: default
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: Role, name: chatbot-agentsandbox}
subjects:
  - {kind: ServiceAccount, name: YOUR-BACKEND-SA, namespace: YOUR-BACKEND-NS}
EOF
```

Then verify, because `kubectl apply` accepts a wrong API group silently:

```bash
kubectl auth can-i get sandboxes        --as=system:serviceaccount:YOUR-BACKEND-NS:YOUR-BACKEND-SA -n default
kubectl auth can-i create sandboxclaims --as=system:serviceaccount:YOUR-BACKEND-NS:YOUR-BACKEND-SA -n default
```

Both grants are load-bearing, and the second one is easy to mistake for
decoration. Tested by removing each:

| Missing | Symptom |
|---|---|
| The whole binding | `403 Forbidden` on the first call |
| `sandboxes` read only | **Every command returns `502`** — the SDK reads `.status.podIPs` from the Sandbox to tell the router where to forward |

## Step 5 — Wire it into your chatbot

```
pip install 'k8s-agent-sandbox[async]==1.0.2'
```

The whole integration. `thread_id` is your conversation/session ID — any
stable string:

```python
import re
import shlex

from k8s_agent_sandbox import AsyncSandboxClient
from k8s_agent_sandbox.exceptions import SandboxRequestError
from k8s_agent_sandbox.models import SandboxDirectConnectionConfig

ROUTER = "http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080"
NAMESPACE = "default"
WARMPOOL = "python-sandbox-pool"
THREAD_LABEL = "chatbot-thread"

client = AsyncSandboxClient(
    # Through the router. Never point this at a pod IP directly — pods
    # authenticate nothing, so pod-direct traffic bypasses the only
    # checkpoint that exists.
    connection_config=SandboxDirectConnectionConfig(api_url=ROUTER, server_port=8888),
)


def _label(thread_id: str) -> str:
    """Label values are <=63 chars from a restricted charset."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", thread_id)[:63]
    return safe.strip("-_.") or "unknown"


async def _sandbox_for(thread_id: str):
    """One pod per conversation — this lookup IS the isolation mechanism."""
    label = _label(thread_id)
    existing = await client.list_all_sandboxes(NAMESPACE, label_selector=f"{THREAD_LABEL}={label}")
    if existing:
        return await client.get_sandbox(existing[0], NAMESPACE)
    return await client.create_sandbox(
        warmpool=WARMPOOL,
        namespace=NAMESPACE,
        sandbox_ready_timeout=180,
        labels={THREAD_LABEL: label},
    )


async def run_bash(thread_id: str, command: str) -> dict:
    sandbox = await _sandbox_for(thread_id)
    # Upstream's stock image runs commands through shlex.split() + subprocess
    # with no shell, so &&, |, > and heredocs all misbehave. Wrapping makes
    # that split yield ['bash', '-c', '<command>'] so bash parses the rest.
    try:
        result = await sandbox.commands.run("bash -c " + shlex.quote(command), timeout=300)
    except SandboxRequestError as e:
        return {"error": str(e), "status": e.status_code}
    return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.exit_code}


async def read_file(thread_id: str, name: str) -> bytes:
    sandbox = await _sandbox_for(thread_id)
    return await sandbox.files.read(name)


async def reset(thread_id: str) -> None:
    """Call when a conversation ends — otherwise pods accumulate."""
    label = _label(thread_id)
    for claim in await client.list_all_sandboxes(NAMESPACE, label_selector=f"{THREAD_LABEL}={label}"):
        await client.delete_sandbox(claim, NAMESPACE)
```

Two things that catch people out:

- **Catch `SandboxRequestError`, not `httpx.HTTPError`.** The SDK uses
  `httpx` internally but wraps every non-2xx in its own type, carrying
  `.status_code`. Code catching the httpx types never matches.
- **Don't assume a working directory.** The stock image has no
  `/workspace`; commands land wherever the image puts them. Use relative
  paths.

## Step 6 — *(optional)* a richer runtime image

Upstream's stock sandbox is a plain Python runtime. If your chatbot
generates documents or charts, this repo's image adds pandas, numpy,
matplotlib, python-docx and pandoc, speaking the same wire protocol
(`GET /`, `POST /execute`, `POST /upload`, `GET /download/<path>` on
port 8888).

```bash
git clone <this-repo-url> jarvis-sandbox && cd jarvis-sandbox

# The toolchain base. --provenance=false avoids an OCI manifest-list some
# registries mishandle — cheap to always pass.
docker build --provenance=false -t ${REGISTRY}/sandbox-base:${TAG} .

# Thin layer swapping in the agent-sandbox-compatible server.
docker build --provenance=false -f Dockerfile.agentsandbox \
  --build-arg BASE_IMAGE=${REGISTRY}/sandbox-base:${TAG} \
  -t ${REGISTRY}/sandbox-runtime:${TAG} .

docker push ${REGISTRY}/sandbox-runtime:${TAG}
```

Then point the Template's `image:` at it and re-apply Step 3. Nothing else
changes — same protocol, same port, same client code.

## Verify

```bash
kubectl get pods -n agent-sandbox-system   # controller + router Running
kubectl get sandboxwarmpool -n default     # READY matches DESIRED
```

Then, from your backend, prove the property that actually matters — two
different `thread_id`s must not see each other's files:

```python
await run_bash("conv-A", "echo 'private to A' > a.txt")
print(await run_bash("conv-A", "ls"))            # includes a.txt
print(await run_bash("conv-B", "ls"))            # must NOT include a.txt
print(await run_bash("conv-B", "cat a.txt"))     # must fail: No such file
print(await run_bash("conv-A", "cat a.txt"))     # reattaches: private to A
```

Leftover claims each hold a pod, so check after failed runs:

```bash
kubectl get sandboxclaim -n default
```

## Troubleshooting

- **Every command returns `502`.** The router can't reach the pod. Check
  the Sandbox has `.status.podIPs` populated, and that your backend's
  ServiceAccount can read `sandboxes` — missing that grant produces exactly
  this, since the SDK is what supplies the pod IP.
- **`&&`, pipes or heredocs behave strangely.** The `bash -c` wrapping in
  `run_bash` is missing. The stock image has no shell in the loop.
- **A `502` reported as a timeout.** If you collapse every
  `SandboxRequestError` into a timeout result, a routing or permission
  failure will surface as "command timed out" and send you debugging the
  wrong thing. Branch on `e.status_code`.
- **`ImagePullBackOff` on sandbox pods.** Nodes can't reach your registry.
  On a local cluster, skip it: build with a local tag and
  `minikube image load` (or your cluster's equivalent).
- **Pull works via `docker pull` but the cluster gets `denied: access
  forbidden`.** Seen against more than one registry; root cause never
  pinned down, and it looked registry-side rather than credential-related.
  Workaround: pull locally, retag to whatever name your nodes resolve, and
  load the image into the cluster directly.
- **Sandboxes can still reach the internet, or each other, despite a
  NetworkPolicy.** Your CNI isn't enforcing NetworkPolicy. Confirm by
  `kubectl exec`ing into a sandbox and curling something external — if it
  works, the policy is inert and you do not have network isolation.
- **Pods accumulate.** Your backend isn't calling `reset()` when
  conversations end.

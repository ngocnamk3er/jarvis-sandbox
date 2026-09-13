# Deploying jarvis-sandbox for a different chatbot

A from-zero guide for running jarvis-sandbox as the code-execution sandbox
behind **any** chatbot backend, not just jarvis-backend — for example, an
existing chatbot at your company that doesn't have a sandbox yet. Every
manifest here is self-contained (no dependency on the `jarvis-deploy` repo
or this workspace's home-lab GitLab/minikube setup) — swap in your own
registry and cluster.

For *why* it's built this way (pod-per-conversation, no in-pod jail,
NetworkPolicy egress lockdown, etc.), see [README.md](README.md)'s "Theory"
section — this guide is action-only.

## Architecture, in one paragraph

One image, two roles. The **orchestrator** is a single Deployment your
chatbot backend calls over HTTP; it maps `thread_id → pod`, creating one
disposable **agent** pod per conversation on demand and proxying commands to
it. Your chatbot never talks to agent pods directly — only to the
orchestrator's Service, with a shared secret header.

## Prerequisites

- A Kubernetes cluster (1.24+), any distro — managed (EKS/GKE/AKS) or
  self-hosted, `kubectl` pointed at it with permission to create
  Deployments/Services/RBAC/NetworkPolicies in one namespace.
- A container registry your cluster's nodes can pull from, and that you can
  `docker push` to.
- Docker (or another OCI builder) locally, to build the image.
- *Recommended, not required:* a NetworkPolicy-enforcing CNI (Calico,
  Cilium, most managed clusters' default CNI already does). Without one the
  egress-lockdown policy below is accepted by the API server but not
  enforced — sandboxes still work, just without that isolation layer until
  you add one.

## Step 1 — Get the code

```bash
git clone <this-repo-url> jarvis-sandbox
cd jarvis-sandbox
```

## Step 2 — Build & push the image

```bash
REGISTRY=your-registry.example.com/your-org   # <- change this
TAG=$(git rev-parse --short HEAD)

# --provenance=false avoids an OCI-manifest-list some registries mishandle
# (multiple registries were observed rejecting cross-repo blob mounts or
# denying pulls of a provenance-attested manifest — cheap to always pass).
docker build --provenance=false -t ${REGISTRY}/jarvis-sandbox:${TAG} .
docker push ${REGISTRY}/jarvis-sandbox:${TAG}
```

One image serves both roles — `SANDBOX_ROLE` (env var, set below) picks
which one a given pod runs. The orchestrator creates agent pods reading
**its own** running image off the k8s API (`SANDBOX_IMAGE` left empty), so
you only ever push/reference this one tag, never two.

## Step 3 — Namespace + secret

```bash
NAMESPACE=jarvis-sandbox   # <- pick any namespace; used throughout below
kubectl create namespace ${NAMESPACE}

# Shared secret between your chatbot backend and the orchestrator — your
# backend sends this back on every call as X-Internal-Api-Key.
kubectl create secret generic jarvis-secrets -n ${NAMESPACE} \
  --from-literal=INTERNAL_API_KEY=$(openssl rand -hex 32)
```

Keep the generated key — your chatbot backend needs the same value (Step 6).

## Step 4 — Apply the manifests

Everything below is one `kubectl apply -f -`. Replace `${REGISTRY}`,
`${TAG}`, `${NAMESPACE}` first (or `envsubst` it).

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: sandbox-config
  namespace: ${NAMESPACE}
data:
  # Consumed by the ORCHESTRATOR only. Agent pods get their env from the pod
  # spec the orchestrator writes (app/orchestrator/podspec.py), derived from
  # these same values — one source of truth, no drift between the two.
  SANDBOX_ROLE: "orchestrator"
  APP_NAME: "Jarvis Sandbox"
  API_PREFIX: "/api/v1"
  POOL_SIZE: "0"              # 0 = pure on-demand, no idle pods kept warm
  MAX_SANDBOXES: "6"          # hard ceiling on total agent pods (past it -> 503)
  IDLE_GC_MINUTES: "30"       # reap a sandbox no exec call touched for this long
  SANDBOX_TTL_MINUTES: "180"  # hard per-pod lifetime, even if still active
  CLAIM_TIMEOUT_SECONDS: "40"
  COMMAND_TIMEOUT_SECONDS: "300"
  SANDBOX_CPU_REQUEST: "100m"
  SANDBOX_CPU_LIMIT: "2"
  SANDBOX_MEM_REQUEST: "256Mi"
  SANDBOX_MEM_LIMIT: "2Gi"
  SANDBOX_WORKSPACE_SIZE: "2Gi"
  SANDBOX_TMP_SIZE: "1Gi"
  SANDBOX_RUNTIME_CLASS: ""   # set to "gvisor" once that RuntimeClass exists on your cluster
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: jarvis-sandbox-orchestrator
  namespace: ${NAMESPACE}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: jarvis-sandbox-orchestrator
  namespace: ${NAMESPACE}
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch", "create", "delete", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: jarvis-sandbox-orchestrator
  namespace: ${NAMESPACE}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: jarvis-sandbox-orchestrator
subjects:
  - kind: ServiceAccount
    name: jarvis-sandbox-orchestrator
    namespace: ${NAMESPACE}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: sandbox
  namespace: ${NAMESPACE}
spec:
  replicas: 1
  strategy:
    type: Recreate   # exactly one orchestrator — two would race on the pool
  selector:
    matchLabels: {app: sandbox}
  template:
    metadata:
      labels: {app: sandbox}
    spec:
      serviceAccountName: jarvis-sandbox-orchestrator
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: orchestrator
          image: ${REGISTRY}/jarvis-sandbox:${TAG}
          imagePullPolicy: IfNotPresent
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: {drop: ["ALL"]}
          env:
            - name: POD_NAMESPACE
              valueFrom: {fieldRef: {fieldPath: metadata.namespace}}
          envFrom:
            - configMapRef: {name: sandbox-config}
            - secretRef: {name: jarvis-secrets}
          ports: [{containerPort: 8000}]
          resources:
            requests: {cpu: "50m", memory: "128Mi"}
            limits: {cpu: "500m", memory: "512Mi"}
          readinessProbe:
            httpGet: {path: /api/v1/health, port: 8000}
            initialDelaySeconds: 3
            periodSeconds: 10
          livenessProbe:
            httpGet: {path: /api/v1/health, port: 8000}
            initialDelaySeconds: 10
            periodSeconds: 20
---
apiVersion: v1
kind: Service
metadata:
  name: sandbox
  namespace: ${NAMESPACE}
spec:
  selector: {app: sandbox}
  ports: [{port: 8000, targetPort: 8000}]
---
# Locks down the per-conversation agent pods: default-deny both directions,
# two narrow holes — ingress only from the orchestrator, egress only DNS.
# Needs a NetworkPolicy-enforcing CNI (see Prerequisites); inert otherwise.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: sandbox-agent
  namespace: ${NAMESPACE}
spec:
  podSelector:
    matchLabels: {app: jarvis-sandbox-agent}
  policyTypes: [Ingress, Egress]
  ingress:
    - from: [{podSelector: {matchLabels: {app: sandbox}}}]
      ports: [{protocol: TCP, port: 8000}]
  egress:
    - to: [{namespaceSelector: {}, podSelector: {matchLabels: {k8s-app: kube-dns}}}]
      ports: [{protocol: UDP, port: 53}, {protocol: TCP, port: 53}]
```

```bash
kubectl -n ${NAMESPACE} rollout status deploy/sandbox
```

## Step 5 — Verify

```bash
kubectl -n ${NAMESPACE} port-forward svc/sandbox 18080:8000 &
KEY=$(kubectl -n ${NAMESPACE} get secret jarvis-secrets -o jsonpath='{.data.INTERNAL_API_KEY}' | base64 -d)

curl -s localhost:18080/api/v1/health
curl -s localhost:18080/api/v1/sandbox/exec -H "X-Internal-Api-Key: $KEY" \
  -H content-type:application/json \
  -d '{"thread_id":"smoke-test","command":"python3 -c \"print(6*7)\" && whoami"}'
# -> {"stdout":"42\nsandboxuser\n","stderr":"","exit_code":0,"timed_out":false}

curl -s -X POST localhost:18080/api/v1/sandbox/reset -H "X-Internal-Api-Key: $KEY" \
  -H content-type:application/json -d '{"thread_id":"smoke-test"}'
```

If `exec` hangs or 502s, check `kubectl -n ${NAMESPACE} get pods` — the
first call creates an agent pod from scratch (~3–5s); a pod stuck in
`ImagePullBackOff` means the cluster's nodes can't reach your registry (see
Troubleshooting).

## Step 6 — Wire it into your chatbot backend

Three routes, all behind `X-Internal-Api-Key: <the secret from Step 3>`.
Point your chatbot's HTTP client at `http://sandbox.${NAMESPACE}.svc.cluster.local:8000`
(in-cluster DNS) if the chatbot backend runs in the same cluster — otherwise
expose the Service via Ingress/LoadBalancer and use that instead.

| route | when to call it | request | response |
|---|---|---|---|
| `POST /api/v1/sandbox/exec` | your "run bash/code" tool | `{"thread_id": "<conversation id>", "command": "<shell command>", "timeout_seconds": 300}` | `{"stdout": "...", "stderr": "...", "exit_code": 0, "timed_out": false}` |
| `GET /api/v1/sandbox/read?thread_id=&name=` | serving a file the sandbox generated | — | raw file bytes, `Content-Disposition` set |
| `POST /api/v1/sandbox/reset` | conversation ends / user stops it | `{"thread_id": "<conversation id>"}` | `{"ok": true}` |

`thread_id` is just your conversation/session ID — any stable string per
conversation. Same `thread_id` across calls reuses the same pod (and its
`/workspace` files); a new `thread_id` gets a fresh pod. Error codes worth
handling explicitly: `503` (at `MAX_SANDBOXES` capacity — retry later),
`504` (pod didn't become Ready within `CLAIM_TIMEOUT_SECONDS`), `502`
(agent pod unreachable — orchestrator will create a new one on the next
call).

Minimal Python client (adapt to whatever your chatbot's tool-calling layer
looks like):

```python
import httpx

SANDBOX_URL = "http://sandbox.jarvis-sandbox.svc.cluster.local:8000/api/v1"
INTERNAL_API_KEY = "..."  # same value as the jarvis-secrets Secret

async def run_bash(thread_id: str, command: str, timeout: int = 300) -> dict:
    async with httpx.AsyncClient(timeout=timeout + 30) as client:
        resp = await client.post(
            f"{SANDBOX_URL}/sandbox/exec",
            json={"thread_id": thread_id, "command": command, "timeout_seconds": timeout},
            headers={"X-Internal-Api-Key": INTERNAL_API_KEY},
        )
        resp.raise_for_status()
        return resp.json()  # {stdout, stderr, exit_code, timed_out}
```

## Tuning for your own scale

All in the `sandbox-config` ConfigMap, edit and re-apply (no image rebuild
needed):

- `MAX_SANDBOXES` — hard ceiling on concurrent agent pods. Set based on
  node capacity (`SANDBOX_MEM_LIMIT` × `MAX_SANDBOXES` should fit
  comfortably under what your nodes provide).
- `POOL_SIZE` — set above `0` to keep N pods pre-warmed if the ~3–5s
  first-call cold start matters for your chatbot's UX; costs `POOL_SIZE ×
  SANDBOX_MEM_REQUEST` sitting idle at all times.
- `SANDBOX_TTL_MINUTES` / `IDLE_GC_MINUTES` — lower these for a
  higher-turnover chatbot (frees pods faster) or raise for long-running
  conversations that shouldn't lose their `/workspace` mid-session.
- `SANDBOX_RUNTIME_CLASS: "gvisor"` — if you want kernel-level isolation
  between sandboxes (not just namespaces), install the gVisor RuntimeClass
  on your cluster first, then set this.

The baked-in toolchain (pandas, matplotlib, python-docx, pandoc, etc. — see
[README.md](README.md#the-image)) is fixed at build time; to add a library,
add it to `Dockerfile` and rebuild/repush (Step 2), then roll the
Deployment.

## Troubleshooting

- **`ImagePullBackOff` on the `sandbox` pod.** Nodes can't reach
  `${REGISTRY}`. If your cluster is local (minikube/kind), skip the
  registry entirely: `docker build ... -t jarvis-sandbox:local . && minikube
  image load jarvis-sandbox:local`, then reference `jarvis-sandbox:local` in
  the Deployment instead of a registry path.
- **Pull works manually (`docker pull`) but the cluster still gets `denied:
  access forbidden`.** Seen against more than one registry, root cause
  never pinned down (looked like a registry-side quirk on certain
  pushes, not a credentials problem — manual JWT replay of the exact same
  auth → manifest → blob sequence succeeded every time). If it recurs:
  `docker pull ${REGISTRY}/jarvis-sandbox:${TAG}` locally, retag to
  whatever name your cluster's nodes resolve for that registry, and
  `minikube image load` (or your cluster's equivalent local-image-load path)
  as a bypass — this is a registry pull-path issue, not a bug in the image.
- **NetworkPolicy applied but agent pods can still reach the internet.**
  Your CNI isn't enforcing NetworkPolicy (default on some clusters/plugins).
  Confirm with `kubectl exec` into an agent pod and `curl` something
  external — if it succeeds, switch/enable a NetworkPolicy-enforcing CNI.
- **`exec` returns 503 immediately.** At `MAX_SANDBOXES`; either raise the
  ConfigMap value or your chatbot is leaking sandboxes — check it's calling
  `/sandbox/reset` when conversations actually end.

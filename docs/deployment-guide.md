# Aegis-OS Deployment Guide

**Audience:** Platform engineers and DevOps teams deploying Aegis-OS to non-local environments  
**Version:** 0.1.0

This guide covers production-grade deployment on Kubernetes, production Vault configuration, TLS, secrets management, and scaling considerations. The Docker Compose setup described in the README is for local development only.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Deployment Architecture](#deployment-architecture)
- [Docker Compose (Development Only)](#docker-compose-development-only)
- [Kubernetes Deployment](#kubernetes-deployment)
- [Vault Production Configuration](#vault-production-configuration)
- [TLS Configuration](#tls-configuration)
- [Secrets Injection](#secrets-injection)
- [OPA Bundle Configuration](#opa-bundle-configuration)
- [Observability Stack](#observability-stack)
- [Health Checks & Readiness](#health-checks--readiness)
- [Scaling Considerations](#scaling-considerations)
- [Pre-Deployment Checklist](#pre-deployment-checklist)

---

## Prerequisites

| Tool | Minimum Version | Purpose |
|---|---|---|
| Docker | 24.0+ | Container runtime |
| Kubernetes | 1.29+ | Production orchestration |
| Helm | 3.14+ | Kubernetes package management |
| `kubectl` | 1.29+ | Cluster management |
| HashiCorp Vault | 1.17+ | Secrets management (production mode) |
| cert-manager | 1.14+ | Automated TLS certificate management |

---

## Deployment Architecture

```
                    ┌─────────────────────────┐
                    │   Ingress Controller     │
                    │   (Nginx / Traefik)      │
                    │   TLS termination        │
                    └────────────┬────────────┘
                                 │
              ┌──────────────────▼──────────────────┐
              │         aegis-api (Deployment)       │
              │         replicas: 2+                 │
              │         port: 8000                   │
              └──────┬─────────────┬────────────────┘
                     │             │              │
              ┌──────▼──┐   ┌─────▼───┐   ┌─────▼──────┐
              │   OPA   │   │  Vault  │   │  Temporal  │
              │ sidecar │   │  Agent  │   │  Worker    │
              │ :8181   │   │         │   │  :7233     │
              └─────────┘   └─────────┘   └────────────┘
                                │
                         ┌──────▼──────┐
                         │  Vault      │
                         │  Cluster    │
                         │  (HA mode)  │
                         └─────────────┘
```

---

## Docker Compose (Development Only)

The `docker-compose.yml` is suitable for **local development and CI only**. It uses:
- Vault in dev mode (no persistence, static root token)
- No TLS
- No authentication on the Aegis API
- In-memory OPA (no bundle signing)

### Services

The compose file starts eight services:

| Service | Port(s) | Purpose |
|---|---|---|
| `aegis-api` | `18000` | Aegis API server (FastAPI / uvicorn) |
| `vault` | `8210` → `8200` | HashiCorp Vault in dev mode (root token: `aegis-dev-root`) |
| `temporal` | `7233` (gRPC), `8088` (HTTP) | Temporal workflow server |
| `temporal-ui` | `18080` | Temporal web UI |
| `postgresql` | internal | Temporal persistence — not exposed to the host; Temporal connects via the internal network |
| `opa` | `8181` | OPA policy engine — mounts `./policies` and auto-loads all `*.rego` files |
| `prometheus` | `19090` | Prometheus metrics scraping |
| `code-scalpel` | `18090` | Code Scalpel MCP SSE server — `http://localhost:18090/sse` |
| `grafana` | `13000` | Grafana dashboards (default user: admin / admin) |

### Startup

```bash
docker-compose up -d
```

Wait for the API to become healthy (usually < 15 s on first run after images are pulled):

```bash
curl -f http://localhost:18000/health
# {"status": "ok"}
```

### Verify OPA loaded all policies

```bash
curl -s http://localhost:8181/v1/policies | python3 -m json.tool | grep '"id"'
# "id": "agent_access"
# "id": "budget"
```

### Stream logs

```bash
docker-compose logs -f aegis-api
```

To run integration tests against the full stack:

```bash
docker-compose up -d
pytest tests/ -v
docker-compose down
```

---

## Kubernetes Deployment

### Namespace

```bash
kubectl create namespace aegis-system
```

### ConfigMap — Non-Secret Configuration

```yaml
# k8s/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: aegis-config
  namespace: aegis-system
data:
  AEGIS_ENV: "production"
  AEGIS_OPA_URL: "http://opa-svc.aegis-system.svc.cluster.local:8181"
  AEGIS_TEMPORAL_HOST: "temporal.aegis-system.svc.cluster.local:7233"
  AEGIS_VAULT_ADDR: "https://vault.vault-system.svc.cluster.local:8200"
  AEGIS_MAX_AGENT_STEPS: "10"
  AEGIS_MAX_TOKEN_VELOCITY: "10000"
  AEGIS_BUDGET_LIMIT_USD: "10.0"
  AEGIS_TOKEN_EXPIRY_SECONDS: "900"
  AEGIS_TOKEN_ALGORITHM: "HS256"
```

### Secret — Sensitive Values

Do **not** create this manually in production. Use Vault Agent Injector or External Secrets Operator (see [Secrets Injection](#secrets-injection)).

```yaml
# k8s/secret.yaml  ← DO NOT COMMIT; manage via Vault or ESO
apiVersion: v1
kind: Secret
metadata:
  name: aegis-secrets
  namespace: aegis-system
type: Opaque
stringData:
  AEGIS_TOKEN_SECRET_KEY: "<generate with: python -c 'import secrets; print(secrets.token_hex(32))'>"
  AEGIS_VAULT_TOKEN: "<scoped vault token — not root>"
```

### Deployment

```yaml
# k8s/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aegis-api
  namespace: aegis-system
spec:
  replicas: 2
  selector:
    matchLabels:
      app: aegis-api
  template:
    metadata:
      labels:
        app: aegis-api
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8000"
        prometheus.io/path: "/metrics"
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
      containers:
        - name: aegis-api
          image: ghcr.io/3D-Tech-Solutions/aegis-os:0.1.0
          ports:
            - containerPort: 8000
          envFrom:
            - configMapRef:
                name: aegis-config
            - secretRef:
                name: aegis-secrets
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 15
            periodSeconds: 30
          resources:
            requests:
              cpu: "250m"
              memory: "256Mi"
            limits:
              cpu: "1000m"
              memory: "512Mi"
          securityContext:
            readOnlyRootFilesystem: true
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
```

### Service

```yaml
# k8s/service.yaml
apiVersion: v1
kind: Service
metadata:
  name: aegis-api-svc
  namespace: aegis-system
spec:
  selector:
    app: aegis-api
  ports:
    - port: 8000
      targetPort: 8000
```

### Ingress (Nginx)

```yaml
# k8s/ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: aegis-ingress
  namespace: aegis-system
  annotations:
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
    nginx.ingress.kubernetes.io/limit-rps: "100"
spec:
  tls:
    - hosts:
        - aegis.yourdomain.com
      secretName: aegis-tls-cert
  rules:
    - host: aegis.yourdomain.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: aegis-api-svc
                port:
                  number: 8000
```

Apply all manifests:

```bash
kubectl apply -f k8s/
```

---

## Vault Production Configuration

The `docker-compose.yml` Vault configuration (`server -dev`) must not be used in production.

### Initialize Vault

```bash
# Initialize with 5 key shares, 3 required to unseal
vault operator init -key-shares=5 -key-threshold=3

# Unseal (run 3 times with different keys)
vault operator unseal <key-1>
vault operator unseal <key-2>
vault operator unseal <key-3>
```

### Create Aegis AppRole

```bash
# Enable AppRole auth method
vault auth enable approle

# Create Aegis policy
vault policy write aegis-policy - <<EOF
path "secret/data/aegis/*" {
  capabilities = ["read"]
}
path "transit/sign/aegis-token-key" {
  capabilities = ["update"]
}
path "transit/verify/aegis-token-key" {
  capabilities = ["update"]
}
EOF

# Create AppRole
vault write auth/approle/role/aegis-api \
  token_policies="aegis-policy" \
  token_ttl=1h \
  token_max_ttl=4h

# Retrieve Role ID and Secret ID for the Kubernetes secret
vault read auth/approle/role/aegis-api/role-id
vault write -f auth/approle/role/aegis-api/secret-id
```

### Store Application Secrets in Vault KV

```bash
vault secrets enable -path=secret kv-v2

vault kv put secret/aegis/api \
  token_secret_key="$(python -c 'import secrets; print(secrets.token_hex(32))')" \
  openai_api_key="sk-..." \
  anthropic_api_key="sk-ant-..."
```

---

## TLS Configuration

Use `cert-manager` with Let's Encrypt for automated certificate management:

```bash
helm repo add jetstack https://charts.jetstack.io
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --set installCRDs=true
```

```yaml
# k8s/clusterissuer.yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ops@yourdomain.com
    privateKeySecretRef:
      name: letsencrypt-prod-key
    solvers:
      - http01:
          ingress:
            class: nginx
```

Reference the issuer in your `Ingress` resource via the `cert-manager.io/cluster-issuer: letsencrypt-prod` annotation.

For **internal service-to-service mTLS** (OPA, Vault, Temporal), use [SPIFFE/SPIRE](https://spiffe.io/) to issue workload identity certificates automatically. This is a Phase 4 target.

---

## Secrets Injection

### Option A: External Secrets Operator (Recommended)

[External Secrets Operator](https://external-secrets.io/) syncs Vault secrets into Kubernetes Secrets automatically, without storing secrets in manifests or CI pipelines.

```bash
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets \
  -n external-secrets-system --create-namespace
```

```yaml
# k8s/external-secret.yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: aegis-secrets
  namespace: aegis-system
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: vault-backend
    kind: ClusterSecretStore
  target:
    name: aegis-secrets
  data:
    - secretKey: AEGIS_TOKEN_SECRET_KEY
      remoteRef:
        key: secret/aegis/api
        property: token_secret_key
```

### Option B: Vault Agent Injector

Annotate the Deployment pod spec to have Vault Agent inject secrets as files:

```yaml
annotations:
  vault.hashicorp.com/agent-inject: "true"
  vault.hashicorp.com/role: "aegis-api"
  vault.hashicorp.com/agent-inject-secret-aegis: "secret/data/aegis/api"
```

---

## OPA Bundle Configuration

In production, OPA should load policies from a signed bundle rather than raw files:

```bash
# Build a signed bundle
opa build policies/ --signing-key ./opa-signing-key.pem -o bundle.tar.gz

# Serve the bundle via an HTTPS endpoint (e.g., S3, GCS, or Nginx)
# Configure OPA to pull from the bundle server:
```

```yaml
# opa-config.yaml
bundles:
  aegis:
    resource: "https://bundles.yourdomain.com/aegis/bundle.tar.gz"
    signing:
      keyid: aegis-bundle-key
      scope: write
    polling:
      min_delay_seconds: 60
      max_delay_seconds: 120
```

This enables policy updates without restarting OPA or the Aegis API.

---

## Observability Stack

### Prometheus Scrape Configuration

The `docs/prometheus.yml` file in this repo is pre-configured to scrape the Aegis API at `aegis-api:8000/metrics`. In Kubernetes, use the `prometheus.io/scrape` pod annotations (already included in the Deployment manifest above) with the Prometheus Operator.

### Grafana Dashboards

Import the following pre-built dashboards into Grafana after connecting your Prometheus data source:

| Dashboard | Purpose |
|---|---|
| Aegis Cost per Department | `aegis_tokens_consumed_total` by `agent_type` mapped to cost |
| Agent Failure Rates | `BudgetExceededError` rate, `LoopDetectedError` rate |
| Policy Violation Heatmap | OPA deny rate over time |
| Session Token Velocity | Active sessions, tokens/minute |

Dashboard JSON definitions will be provided in `docs/grafana/` in a future release.

### Alerting Rules

Add the following rules to your Prometheus `rules.yaml`:

```yaml
groups:
  - name: aegis
    rules:
      - alert: HighTokenVelocity
        expr: rate(aegis_tokens_consumed_total[5m]) > 500
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "High token velocity detected"

      - alert: BudgetCritical
        expr: aegis_budget_remaining_usd < 1.0
        for: 0m
        labels:
          severity: critical
        annotations:
          summary: "Agent session budget nearly exhausted"

      - alert: OPAHighErrorRate
        expr: rate(aegis_opa_errors_total[5m]) > 0.1
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "OPA evaluation errors — policy enforcement may be degraded"
```

---

## Health Checks & Readiness

| Endpoint | Type | Expected Response |
|---|---|---|
| `GET /health` | Liveness | `{"status": "ok", "service": "aegis-os"}` |
| `GET /metrics` | Prometheus scrape | Prometheus text format |

A **deep health check** (verifying OPA reachability and Vault connectivity) is planned for Phase 1 as `GET /health/ready`.

---

## Scaling Considerations

| Component | Horizontal Scaling | Notes |
|---|---|---|
| `aegis-api` | ✅ Stateless (v0.1) | In-memory budget/loop state means sessions are node-local until Phase 2 Temporal migration. Use sticky sessions or co-locate sessions with the same replica if precise tracking is required before Phase 2. |
| OPA | ✅ Read-heavy, stateless | Scale as a standard Deployment; use a bundle server rather than file mounts for consistent policy across replicas. |
| Vault | ✅ HA mode with Raft | Use the integrated Raft storage backend for production HA. |
| Temporal | ✅ Clustered | Temporal scales independently; refer to the [Temporal production deployment guide](https://docs.temporal.io/cluster-deployment-guide). |
| PostgreSQL (Temporal) | ✅ Read replicas | Primary for Temporal write-ahead log; read replicas for history queries. |

---

## Pre-Deployment Checklist

Complete every item before routing production traffic to a new Aegis-OS deployment.

### Secrets & Identity
- [ ] `AEGIS_TOKEN_SECRET_KEY` is a 256-bit random value stored in Vault, not an environment variable
- [ ] Vault is initialized in production mode (not `-dev`); unseal keys are distributed across key holders
- [ ] Aegis uses an AppRole with a scoped policy — never the Vault root token
- [ ] All LLM provider API keys are stored in Vault KV, not env vars

### Network & TLS
- [ ] TLS 1.3 enforced on all external endpoints
- [ ] `/metrics` endpoint is firewalled to Prometheus scraper IP only
- [ ] OPA (8181), Vault (8200), Temporal (7233), PostgreSQL (5432) are not exposed outside the cluster network

### Policy & Governance
- [ ] `opa test policies/ -v` passes with 0 failures
- [ ] OPA is configured to load a signed bundle (not raw files) in production
- [ ] Fail-closed behavior verified: stop OPA, confirm API returns 503

### Observability
- [ ] Prometheus is scraping `/metrics` and the `aegis_tokens_consumed_total` counter is incrementing
- [ ] Grafana dashboards are visible and showing live data
- [ ] Alerting rules for `HighTokenVelocity` and `BudgetCritical` are active
- [ ] Structured logs are flowing to the log aggregator (not just stdout)

### Container Hardening
- [ ] Container runs as UID 1000 (non-root)
- [ ] `readOnlyRootFilesystem: true` is set
- [ ] All capabilities dropped (`capabilities.drop: ["ALL"]`)
- [ ] Container image is pinned by SHA256 digest in the Deployment manifest

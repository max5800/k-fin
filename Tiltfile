# Tiltfile for K-Fin
# Usage:
#   tilt up --stream                          (remote dev stage — preferred)
#   tilt up --stream -- --profile=local       (local Rancher Desktop — legacy)

config.define_string("profile", usage="Deployment profile: 'local' or 'remote'")
cfg = config.parse()
profile = cfg.get("profile", "remote")

# Release name — must match helm() below
RELEASE_NAME = "k-fin-dev"
FULLNAME = RELEASE_NAME + "-k-fin"
SECRET_NAME = FULLNAME + "-comdirect-secrets"

# Select K8s context and values based on profile
if profile == "remote":
    allow_k8s_contexts("k3s-app")
    k8s_namespace("comdirect")
    values_file = "dev/values.remote.yaml"
    ingress_host = "k-fin-dev.max5800.com"
    docs_base = "https://" + ingress_host
else:
    allow_k8s_contexts("rancher-desktop")
    values_file = "dev/values.local.yaml"
    ingress_host = "k-fin-dev.local"
    docs_base = "http://localhost:8000"

    # Local dev: create K8s Secret from .env file
    # (remote uses ExternalSecret/Vault instead)
    local_resource(
        "create-secrets",
        cmd="kubectl delete secret " + SECRET_NAME + " --ignore-not-found"
            + " && kubectl create secret generic " + SECRET_NAME + " --from-env-file=.env",
        labels=["secrets"],
        deps=[".env"],
    )

# Registry
REGISTRY_API = "ghcr.io/max5800/k-fin-api"
REGISTRY_WORKER = "ghcr.io/max5800/k-fin-worker"
REGISTRY_MIGRATE = "ghcr.io/max5800/k-fin-migrate"

# Build API image (lean, no bank secrets)
docker_build(
    REGISTRY_API,
    context=".",
    dockerfile="./Dockerfile.api",
    entrypoint=["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--reload"],
    live_update=[
        sync("./src/api", "/app/src/api"),
        sync("./src/core", "/app/src/core"),
        sync("./src/agents", "/app/src/agents"),
    ],
)

# Build Worker image (full, has bank credentials access)
docker_build(
    REGISTRY_WORKER,
    context=".",
    dockerfile="./Dockerfile",
    entrypoint=["/app/.venv/bin/uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--reload"],
    live_update=[
        sync("./src", "/app/src"),
        sync("./main.py", "/app/main.py"),
        sync("./scripts", "/app/scripts"),
    ],
)

# Build Migrate image (same Dockerfile, no entrypoint override — chart's command wins)
docker_build(
    REGISTRY_MIGRATE,
    context=".",
    dockerfile="./Dockerfile",
)

# Deploy Helm chart
k8s_yaml(helm(
    "./chart",
    name=RELEASE_NAME,
    namespace="comdirect" if profile == "remote" else "",
    values=[values_file],
    set=[
        "api.image.repository=" + REGISTRY_API,
        "worker.image.repository=" + REGISTRY_WORKER,
        "postgres.migrate.image.repository=" + REGISTRY_MIGRATE,
        "ingress.host=" + ingress_host,
    ],
))

# API resource: public-facing, port-forwarded for local dev
k8s_resource(
    FULLNAME + "-api",
    port_forwards=["8000:8000"] if profile == "local" else [],
    labels=["k-fin"],
    links=[
        link(docs_base + "/docs", "Swagger UI"),
        link(docs_base + "/redoc", "ReDoc"),
        link(docs_base + "/health", "Health"),
    ],
    resource_deps=["create-secrets"] if profile == "local" else [],
)

# Worker resource: internal only, no port forwarding
k8s_resource(
    FULLNAME + "-worker",
    labels=["k-fin"],
    resource_deps=["create-secrets"] if profile == "local" else [],
)

# Tiltfile for comdirect-firefly-sync
# Usage:
#   tilt up --stream -- --profile=local    (Rancher Desktop)
#   tilt up --stream -- --profile=remote   (remote K3s app cluster)

config.define_string("profile", usage="Deployment profile: 'local' or 'remote'")
cfg = config.parse()
profile = cfg.get("profile", "local")

# Release name — must match helm() below
RELEASE_NAME = "comdirect-sync"
SECRET_NAME = RELEASE_NAME + "-comdirect-firefly-sync-comdirect-secrets"

# Select K8s context and values based on profile
if profile == "remote":
    allow_k8s_contexts("k3s-app")
    values_file = "dev/values.remote.yaml"
    ingress_host = "comdirect-sync.max5800.com"
else:
    allow_k8s_contexts("rancher-desktop")
    values_file = "dev/values.local.yaml"
    ingress_host = "comdirect-sync.local"

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
REGISTRY = "ghcr.io/max5800/comdirect-firefly-sync"

# Build image with live_update for fast dev cycles
docker_build(
    REGISTRY,
    context=".",
    dockerfile="./Dockerfile",
    entrypoint=["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"],
    live_update=[
        sync("./src", "/app/src"),
        sync("./main.py", "/app/main.py"),
        sync("./scripts", "/app/scripts"),
    ],
)

# Deploy Helm chart
k8s_yaml(helm(
    "./chart",
    name=RELEASE_NAME,
    values=[values_file],
    set=[
        "image.repository=" + REGISTRY,
        "ingress.host=" + ingress_host,
    ],
))

# Configure Tilt resource
k8s_resource(
    RELEASE_NAME + "-comdirect-firefly-sync-api",
    port_forwards=["8000:8000"],
    labels=["sync"],
    links=[
        link("http://localhost:8000/health", "Health Check"),
        link("http://localhost:8000/docs", "API Docs"),
    ],
    resource_deps=["create-secrets"] if profile == "local" else [],
)

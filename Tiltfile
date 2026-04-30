# Tiltfile for K-Fin — remote dev only (k3s-app)
# Usage:
#   tilt up --stream

allow_k8s_contexts("k3s-app")
k8s_namespace("k-fin")

RELEASE_NAME = "k-fin-dev"
FULLNAME = RELEASE_NAME + "-k-fin"
INGRESS_HOST = "k-fin-dev.max5800.com"
DOCS_BASE = "http://" + INGRESS_HOST

UI_CONTEXT = "../k-fin-ui"

# Registry
REGISTRY_API = "ghcr.io/max5800/k-fin-api"
REGISTRY_WORKER = "ghcr.io/max5800/k-fin-worker"
REGISTRY_MIGRATE = "ghcr.io/max5800/k-fin-migrate"
REGISTRY_UI = "ghcr.io/max5800/k-fin-ui"

# Build API image (lean, no bank secrets)
docker_build(
    REGISTRY_API,
    context=".",
    dockerfile="./Dockerfile.api",
    entrypoint=["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--reload"],
    live_update=[
        fall_back_on(["./pyproject.toml", "./uv.lock"]),
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
        fall_back_on(["./pyproject.toml", "./uv.lock"]),
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

# Build UI image (Vite dev-server with HMR via live_update).
# package.json changes fall outside the sync paths and trigger a rebuild
# so `npm ci` re-runs.
docker_build(
    REGISTRY_UI,
    context=UI_CONTEXT,
    dockerfile=UI_CONTEXT + "/Dockerfile.dev",
    live_update=[
        sync(UI_CONTEXT + "/src", "/app/src"),
        sync(UI_CONTEXT + "/index.html", "/app/index.html"),
        sync(UI_CONTEXT + "/vite.config.ts", "/app/vite.config.ts"),
    ],
    ignore=[
        UI_CONTEXT + "/node_modules",
        UI_CONTEXT + "/dist",
        UI_CONTEXT + "/.env.local",
    ],
)

# Deploy Helm chart
k8s_yaml(helm(
    "./chart",
    name=RELEASE_NAME,
    namespace="k-fin",
    values=["dev/values.remote.yaml"],
    set=[
        "api.image.repository=" + REGISTRY_API,
        "worker.image.repository=" + REGISTRY_WORKER,
        "postgres.migrate.image.repository=" + REGISTRY_MIGRATE,
        "ui.image.repository=" + REGISTRY_UI,
        "ingress.host=" + INGRESS_HOST,
    ],
))

# API resource: public-facing, accessed via Ingress
k8s_resource(
    FULLNAME + "-api",
    labels=["k-fin"],
    links=[
        link(DOCS_BASE + "/docs", "Swagger UI"),
        link(DOCS_BASE + "/redoc", "ReDoc"),
        link(DOCS_BASE + "/health", "Health"),
    ],
)

# Worker resource: internal only
k8s_resource(
    FULLNAME + "-worker",
    labels=["k-fin"],
)

# UI resource: accessed via Ingress (path-split: /api → api, / → ui)
k8s_resource(
    FULLNAME + "-ui",
    labels=["k-fin"],
    links=[link(DOCS_BASE, "k-fin UI")],
)

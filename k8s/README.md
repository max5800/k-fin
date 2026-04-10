# Kubernetes Manifests

These manifests are intentionally minimal.

## Design

- `api-deployment.yaml`: always-on read-only API, no bank secrets
- `manual-export-job.yaml`: manually triggered export job, receives `comdirect-secrets`
- `api-service.yaml`: ClusterIP service

## Why no CronJob?

Because Comdirect authentication requires manual pushTAN confirmation.
Running unattended scheduled jobs would be misleading and operationally unsafe in the current design.

## Required prerequisites

- Namespace `comdirect`
- PVC `comdirect-export-data`
- ExternalSecret-generated Secret `comdirect-secrets`
- Built container images pushed to a registry

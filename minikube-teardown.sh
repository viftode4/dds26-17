#!/usr/bin/env bash
set -euo pipefail

# ==========================================
#  Minikube teardown script
#  Removes all deployed resources.
# ==========================================

echo "==> Deleting k8s resources..."
kubectl delete -f k8s/ --ignore-not-found

echo "==> Uninstalling Redis Helm releases..."
helm uninstall order-redis   --ignore-not-found 2>/dev/null || true
helm uninstall stock-redis   --ignore-not-found 2>/dev/null || true
helm uninstall payment-redis --ignore-not-found 2>/dev/null || true

echo "==> Teardown complete."

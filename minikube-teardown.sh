#!/usr/bin/env bash
set -euo pipefail

# ==========================================
#  Minikube teardown script
#  Removes all deployed resources.
# ==========================================

NAMESPACE="distributed-systems"

echo "==> Deleting Application k8s resources..."
kubectl delete -f k8s/nats.yaml        --namespace "${NAMESPACE}" --ignore-not-found
kubectl delete -f k8s/gateway.yaml     --namespace "${NAMESPACE}" --ignore-not-found
kubectl delete -f k8s/order-app.yaml   --namespace "${NAMESPACE}" --ignore-not-found
kubectl delete -f k8s/stock-app.yaml   --namespace "${NAMESPACE}" --ignore-not-found
kubectl delete -f k8s/payment-app.yaml --namespace "${NAMESPACE}" --ignore-not-found

echo "==> Uninstalling Redis Helm releases..."
helm uninstall order-redis   --namespace "${NAMESPACE}" --ignore-not-found 2>/dev/null || true
helm uninstall stock-redis   --namespace "${NAMESPACE}" --ignore-not-found 2>/dev/null || true
helm uninstall payment-redis --namespace "${NAMESPACE}" --ignore-not-found 2>/dev/null || true

echo "==> Deleting namespace '${NAMESPACE}'..."
kubectl delete namespace "${NAMESPACE}" --ignore-not-found

echo "==> Teardown complete."

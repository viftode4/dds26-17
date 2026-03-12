#!/usr/bin/env bash
set -euo pipefail

# ==========================================
#  Minikube full deployment script
#  Builds images, installs Redis via Helm,
#  deploys services and ingress.
# ==========================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NAMESPACE="distributed-systems"
REDIS_CHART_VERSION="5.4.3"

echo "==> Checking minikube status..."
if ! minikube status &>/dev/null; then
    echo "==> Starting minikube..."
    minikube start --memory=8192 --cpus=8
fi

echo "==> Pointing docker to minikube's daemon..."
eval "$(minikube docker-env)"

echo "==> Building Docker images inside minikube..."
# common/, orchestrator/, lua/ live in the repo root and are mounted as volumes
# in docker-compose, but must be baked into the image for k8s. Temporarily copy
# them into each service build context, then remove after build.
for service in order stock payment; do
    cp -r common orchestrator lua "${service}/"
    docker build -t "${service}:latest" "./${service}"
    rm -rf "${service}/common" "${service}/orchestrator" "${service}/lua"
done

echo "==> Creating namespace '${NAMESPACE}'..."
kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

echo "==> Installing Redis clusters via Helm (Sentinel enabled, chart v${REDIS_CHART_VERSION})..."
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update

# Deploy 3 separate Redis clusters
helm upgrade --install \
    -f helm-config/redis-helm-values.yaml \
    --set sentinel.primarySet=order-master \
    --set master.resources.limits.cpu=3000m \
    --set master.resources.requests.cpu=500m \
    --version "${REDIS_CHART_VERSION}" \
    --namespace "${NAMESPACE}" \
    order-redis bitnami/valkey

helm upgrade --install \
    -f helm-config/redis-helm-values.yaml \
    --set sentinel.primarySet=stock-master \
    --version "${REDIS_CHART_VERSION}" \
    --namespace "${NAMESPACE}" \
    stock-redis bitnami/valkey

helm upgrade --install \
    -f helm-config/redis-helm-values.yaml \
    --set sentinel.primarySet=payment-master \
    --version "${REDIS_CHART_VERSION}" \
    --namespace "${NAMESPACE}" \
    payment-redis bitnami/valkey

echo "==> Waiting for Redis pods to be ready..."
kubectl wait --for=condition=ready pod \
    -l app.kubernetes.io/instance=order-redis \
    --namespace "${NAMESPACE}" --timeout=300s
kubectl wait --for=condition=ready pod \
    -l app.kubernetes.io/instance=stock-redis \
    --namespace "${NAMESPACE}" --timeout=300s
kubectl wait --for=condition=ready pod \
    -l app.kubernetes.io/instance=payment-redis \
    --namespace "${NAMESPACE}" --timeout=300s

echo "==> Deploying NATS..."
kubectl apply -f k8s/nats.yaml --namespace "${NAMESPACE}"

echo "==> Deploying Application Services..."
kubectl apply -f k8s/stock-app.yaml   --namespace "${NAMESPACE}"
kubectl apply -f k8s/payment-app.yaml --namespace "${NAMESPACE}"
kubectl apply -f k8s/order-app.yaml   --namespace "${NAMESPACE}"

echo "==> Deploying Gateway..."
kubectl apply -f k8s/gateway.yaml --namespace "${NAMESPACE}"

echo "==> Waiting for service pods to be ready..."
kubectl wait --for=condition=ready pod -l app=order   --namespace "${NAMESPACE}" --timeout=120s
kubectl wait --for=condition=ready pod -l app=stock   --namespace "${NAMESPACE}" --timeout=120s
kubectl wait --for=condition=ready pod -l app=payment --namespace "${NAMESPACE}" --timeout=120s
kubectl wait --for=condition=ready pod -l app=gateway --namespace "${NAMESPACE}" --timeout=120s

MINIKUBE_IP=$(minikube ip)

echo ""
echo "==> Deployment complete!"
echo ""
echo "    Gateway is exposed directly at:"
echo "      http://${MINIKUBE_IP}:30080"
echo ""
echo "    Test with:"
echo "      curl http://${MINIKUBE_IP}:30080/orders/health"
echo "      curl http://${MINIKUBE_IP}:30080/stock/health"
echo "      curl http://${MINIKUBE_IP}:30080/payment/health"
echo ""

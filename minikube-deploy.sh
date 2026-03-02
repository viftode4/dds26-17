#!/usr/bin/env bash
set -euo pipefail

# ==========================================
#  Minikube full deployment script
#  Builds images, installs Redis via Helm,
#  deploys services and ingress.
# ==========================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TX_MODE="${TX_MODE:-saga}"

echo "==> Checking minikube status..."
if ! minikube status &>/dev/null; then
    echo "==> Starting minikube..."
    minikube start
fi

echo "==> Pointing docker to minikube's daemon..."
eval "$(minikube docker-env)"

echo "==> Building Docker images inside minikube..."
docker build -t order:latest   ./order
docker build -t stock:latest   ./stock
docker build -t payment:latest ./payment

echo "==> Installing Redis Helm charts..."
bash deploy-charts-minikube.sh

echo "==> Waiting for Redis pods to be ready..."
kubectl wait --for=condition=ready pod -l app.kubernetes.io/instance=order-redis   --timeout=120s
kubectl wait --for=condition=ready pod -l app.kubernetes.io/instance=stock-redis   --timeout=120s
kubectl wait --for=condition=ready pod -l app.kubernetes.io/instance=payment-redis --timeout=120s

echo "==> Setting TX_MODE=$TX_MODE in order deployment..."
sed "s/value: \"saga\"/value: \"$TX_MODE\"/" k8s/order-app.yaml | kubectl apply -f -

echo "==> Deploying services..."
kubectl apply -f k8s/stock-app.yaml
kubectl apply -f k8s/payment-app.yaml

echo "==> Deploying gateway..."
kubectl apply -f k8s/gateway.yaml

echo "==> Waiting for service pods to be ready..."
kubectl wait --for=condition=ready pod -l app=order   --timeout=120s
kubectl wait --for=condition=ready pod -l app=stock   --timeout=120s
kubectl wait --for=condition=ready pod -l app=payment --timeout=120s
kubectl wait --for=condition=ready pod -l app=gateway --timeout=120s

echo ""
echo "==> Deployment complete!"
echo ""
echo "    Starting port-forward on localhost:8000 ..."
echo "    Test with:"
echo "      curl http://localhost:8000/orders/health"
echo "      curl http://localhost:8000/stock/health"
echo "      curl http://localhost:8000/payment/health"
echo ""
echo "    Press Ctrl+C to stop."
echo ""
kubectl port-forward svc/gateway 8000:80

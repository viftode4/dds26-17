#!/usr/bin/env bash
set -euo pipefail

helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update

helm upgrade --install -f helm-config/redis-helm-values.yaml order-redis bitnami/redis
helm upgrade --install -f helm-config/redis-helm-values.yaml stock-redis bitnami/redis
helm upgrade --install -f helm-config/redis-helm-values.yaml payment-redis bitnami/redis

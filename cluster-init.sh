#!/bin/sh
# One-shot script that forms three independent Redis clusters after all nodes are
# healthy. Run as a Docker Compose service with restart: "no".

set -e

wait_ping() {
    host=$1
    echo "[cluster-init] Waiting for $host..."
    until redis-cli -a redis --no-auth-warning -h "$host" ping 2>/dev/null | grep -q PONG; do
        sleep 1
    done
    echo "[cluster-init] $host is ready"
}

resolve_ip() {
    # getent hosts returns "<ip> <hostname>"; we want the IP
    getent hosts "$1" | awk '{print $1}'
}

# Wait for all 18 cluster nodes to accept connections
for host in \
    order-cluster-1   order-cluster-2   order-cluster-3 \
    order-cluster-replica-1 order-cluster-replica-2 order-cluster-replica-3 \
    stock-cluster-1   stock-cluster-2   stock-cluster-3 \
    stock-cluster-replica-1 stock-cluster-replica-2 stock-cluster-replica-3 \
    payment-cluster-1 payment-cluster-2 payment-cluster-3 \
    payment-cluster-replica-1 payment-cluster-replica-2 payment-cluster-replica-3
do
    wait_ping "$host"
done

echo "[cluster-init] All nodes ready — forming clusters"

# Form order cluster: 3 masters + 3 replicas, 1 replica per master
redis-cli -a redis --no-auth-warning --cluster create \
    "$(resolve_ip order-cluster-1)":6379 \
    "$(resolve_ip order-cluster-2)":6379 \
    "$(resolve_ip order-cluster-3)":6379 \
    "$(resolve_ip order-cluster-replica-1)":6379 \
    "$(resolve_ip order-cluster-replica-2)":6379 \
    "$(resolve_ip order-cluster-replica-3)":6379 \
    --cluster-replicas 1 --cluster-yes

echo "[cluster-init] Order cluster formed"

# Form stock cluster
redis-cli -a redis --no-auth-warning --cluster create \
    "$(resolve_ip stock-cluster-1)":6379 \
    "$(resolve_ip stock-cluster-2)":6379 \
    "$(resolve_ip stock-cluster-3)":6379 \
    "$(resolve_ip stock-cluster-replica-1)":6379 \
    "$(resolve_ip stock-cluster-replica-2)":6379 \
    "$(resolve_ip stock-cluster-replica-3)":6379 \
    --cluster-replicas 1 --cluster-yes

echo "[cluster-init] Stock cluster formed"

# Form payment cluster
redis-cli -a redis --no-auth-warning --cluster create \
    "$(resolve_ip payment-cluster-1)":6379 \
    "$(resolve_ip payment-cluster-2)":6379 \
    "$(resolve_ip payment-cluster-3)":6379 \
    "$(resolve_ip payment-cluster-replica-1)":6379 \
    "$(resolve_ip payment-cluster-replica-2)":6379 \
    "$(resolve_ip payment-cluster-replica-3)":6379 \
    --cluster-replicas 1 --cluster-yes

echo "[cluster-init] Payment cluster formed"
echo "[cluster-init] All clusters ready"

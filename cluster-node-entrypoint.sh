#!/bin/sh
# Resolve the container's own IP for cluster-announce-ip.
# Docker DNS names can resolve differently from different nodes once cluster
# topology shifts, so advertising the IP directly avoids stale-hostname issues.
SELF_IP=$(hostname -i | awk '{print $1}')

cat > /tmp/cluster.conf <<EOF
cluster-enabled yes
cluster-config-file /data/nodes.conf
cluster-node-timeout 5000
cluster-announce-ip ${SELF_IP}
cluster-announce-port 6379
appendonly yes
appendfsync everysec
requirepass redis
masterauth redis
maxmemory ${MAXMEMORY:-200mb}
maxclients ${MAXCLIENTS:-4096}
io-threads ${IO_THREADS:-2}
io-threads-do-reads yes
EOF

# Stock and payment cluster shards require at least one replica in sync before
# acknowledging writes — same durability guarantee as the Sentinel topology.
if [ "${MIN_REPLICAS_TO_WRITE:-0}" = "1" ]; then
    cat >> /tmp/cluster.conf <<EOF
min-replicas-to-write 1
min-replicas-max-lag 10
EOF
fi

exec redis-server /tmp/cluster.conf

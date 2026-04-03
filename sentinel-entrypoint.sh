#!/bin/sh
# Resolve Docker hostnames to IPs at startup time.
# This ensures Sentinel tracks masters by IP, so failover works
# even when the master container is killed and its DNS entry disappears.

ORDER_IP=$(getent hosts order-db | awk '{print $1}')
STOCK_IP=$(getent hosts stock-db | awk '{print $1}')
PAYMENT_IP=$(getent hosts payment-db | awk '{print $1}')
CHECKOUT_IP=$(getent hosts checkout-db | awk '{print $1}')

# Wait until all hosts are resolvable
while [ -z "$ORDER_IP" ] || [ -z "$STOCK_IP" ] || [ -z "$PAYMENT_IP" ] || [ -z "$CHECKOUT_IP" ]; do
    echo "Waiting for Redis masters to be resolvable..."
    sleep 1
    ORDER_IP=$(getent hosts order-db | awk '{print $1}')
    STOCK_IP=$(getent hosts stock-db | awk '{print $1}')
    PAYMENT_IP=$(getent hosts payment-db | awk '{print $1}')
    CHECKOUT_IP=$(getent hosts checkout-db | awk '{print $1}')
done

echo "Resolved: order-db=$ORDER_IP, stock-db=$STOCK_IP, payment-db=$PAYMENT_IP, checkout-db=$CHECKOUT_IP"

cat > /tmp/sentinel.conf <<EOF
port 26379

sentinel monitor order-master $ORDER_IP 6379 2
sentinel auth-pass order-master redis
sentinel down-after-milliseconds order-master 5000
sentinel failover-timeout order-master 10000

sentinel monitor stock-master $STOCK_IP 6379 2
sentinel auth-pass stock-master redis
sentinel down-after-milliseconds stock-master 5000
sentinel failover-timeout stock-master 10000

sentinel monitor payment-master $PAYMENT_IP 6379 2
sentinel auth-pass payment-master redis
sentinel down-after-milliseconds payment-master 5000
sentinel failover-timeout payment-master 10000

sentinel monitor checkout-master $CHECKOUT_IP 6379 2
sentinel auth-pass checkout-master redis
sentinel down-after-milliseconds checkout-master 5000
sentinel failover-timeout checkout-master 10000
EOF

exec redis-sentinel /tmp/sentinel.conf

# Distributed Data Systems

This repository contains a microservices-based distributed system built with Python, Redis, and Docker. The services include `order`, `stock`, and `payment`, fronted by an Nginx gateway.

## Prerequisites
- Docker
- Docker Compose
- [minikube](https://minikube.sigs.k8s.io/docs/start/) (for Kubernetes)
- [kubectl](https://kubernetes.io/docs/tasks/tools/) (for Kubernetes)
- [Helm](https://helm.sh/docs/intro/install/) (for Kubernetes)

## Running the Application

### 1. Build the Docker Images
To build the Docker images for all the microservices, run the following command from the root of the project:

```bash
docker compose build
```

### 2. Start the Services
To start all the services (Nginx gateway, microservices, and their respective Redis databases) in the background, run:

```bash
docker compose up -d
```

The `order` service supports two distributed transaction protocols: SAGA (default) and 2PC (Two-Phase Commit). You can specify which one to use by setting the `TX_MODE` environment variable.

**Start with SAGA (default):**
```bash
TX_MODE=saga docker compose up -d
```

**Start with 2PC:**
```bash
TX_MODE=2pc docker compose up -d
```

### 3. Check Service Status
To verify that all containers are running successfully, use:

```bash
docker compose ps
```
You can also view the logs for a specific service or all services to ensure they started without errors:
```bash
docker compose logs -f
```

### 4. Scaling the Services
To run multiple instances of the microservices (e.g., for load balancing), you can use the `--scale` flag when starting the services. 

For example, to run 3 instances of the `order` service and 2 instances of the `stock` service:
```bash
docker compose up -d --scale order-service=4 --scale stock-service=3 --scale payment-service=3
```
*Note: The Nginx API gateway will automatically load-balance requests across the multiple instances. If you scale services that are already running, you must restart the gateway so it registers the new container IPs:*
```bash
docker compose restart gateway
```

### 5. Stopping the Services
To stop and remove the containers, networks, and volumes (optional), use:
```bash
# To stop and remove containers
docker compose down

# To also remove the data volumes (resets all databases)
docker compose down -v
```

## Interacting with the Services

All services are accessible through the Nginx API gateway, which is exposed on port `8000`. You can interact with the endpoints using `curl`, HTTP clients like Postman, or through scripts.

Examples:

**Check Order Service Health:**
```bash
curl http://localhost:8000/order/health
```

**Create a User (Payment Service):**
```bash
curl -X POST http://localhost:8000/payment/create_user
```

**Create an Item (Stock Service):**
```bash
curl -X POST http://localhost:8000/stock/item/create/10
```

## Running with Kubernetes (minikube)

### 1. Deploy

A single script handles everything: starts minikube, builds images inside the cluster, installs Redis via Helm, and deploys all services.

```bash
./minikube-deploy.sh
```

The script ends by starting a port-forward, making the gateway available on `localhost:8000` — identical to the Docker Compose setup. Press `Ctrl+C` to stop the port-forward (the cluster keeps running).

### 2. Transaction mode

```bash
# SAGA (default)
./minikube-deploy.sh

# SAGA (explicit)
TX_MODE=saga ./minikube-deploy.sh

# 2PC
TX_MODE=2pc ./minikube-deploy.sh
```

### 3. Teardown

```bash
./minikube-teardown.sh
```

This removes all k8s resources and Helm releases. To also delete the minikube cluster entirely:

```bash
minikube delete
```

---

## Running the Tests

The project includes an end-to-end test suite (`test/test_microservices.py`) that interacts with the live system. Ensure the services are running (via `docker compose up -d` or `./minikube-deploy.sh`) before executing the tests.

Run the test suite using Python's built-in `unittest` module:

```bash
python -m unittest test/test_microservices.py
```

*Note: The test suite uses the `test/utils.py` helper methods to make HTTP requests against the gateway on port `8000`.*

## Fault Tolerance Tests

> **These tests require Docker Compose.** They rely on `docker kill` / `docker start` to inject failures into specific containers and cannot be used with the Kubernetes setup.

The fault tolerance suite (`test/test_fault_tolerance.py`) verifies that the system maintains data consistency under random container failures. It runs a **Chaos Monkey** alongside concurrent purchase workloads: a background thread randomly kills and restarts service and Redis replica containers while 40 worker threads simultaneously attempt checkouts.

Containers targeted by the Chaos Monkey:
- `order-service`, `stock-service`, `payment-service`
- `order-db-replica`, `stock-db-replica`, `payment-db-replica`

After the workload completes, the test asserts:
1. Credit spent equals the value of stock actually sold (no money lost or duplicated)
2. Workers never reported more successes than stock was actually deducted

Start the services with Docker Compose first, then run:

```bash
docker compose up -d
python -m unittest test/test_fault_tolerance.py
```

The test takes roughly a minute to complete as it waits 20 seconds at the end for any in-flight async transactions to settle.

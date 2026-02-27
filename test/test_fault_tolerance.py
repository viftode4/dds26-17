import unittest
import threading
import time
import random
import utils as tu
import utils_docker as docker

# Containers to randomly kill
TARGET_CONTAINERS = [
    "distributed-data-systems-order-service-1",
    "distributed-data-systems-stock-service-1",
    "distributed-data-systems-payment-service-1",
    "distributed-data-systems-order-db-replica-1",
    "distributed-data-systems-stock-db-replica-1",
    "distributed-data-systems-payment-db-replica-1",
]


class ChaosMonkey:
    def __init__(self, kill_interval=5):
        self.kill_interval = kill_interval
        self.running = False
        self.chaos_thread = None
        self.killed_containers = []

    def start(self):
        self.running = True
        self.chaos_thread = threading.Thread(target=self._run_chaos)
        self.chaos_thread.start()

    def stop(self):
        self.running = False
        if self.chaos_thread:
            self.chaos_thread.join()
        
        # Ensure all containers are back up
        for container in self.killed_containers:
            try:
                docker.start_container(container)
            except Exception as e:
                print(f"Failed to recover {container}: {e}")

    def _run_chaos(self):
        while self.running:
            time.sleep(self.kill_interval)
            
            # 50% chance to kill a container if none are currently dead
            if random.random() < 0.5 and not self.killed_containers:
                container = random.choice(TARGET_CONTAINERS)
                try:
                    docker.kill_container(container)
                    self.killed_containers.append(container)
                    print(f"[{time.strftime('%X')}] Chaos injected: {container} is down!")
                except Exception as e:
                    print(f"Error killing {container}: {e}")
            
            # If a container was killed, 50% chance to bring it back after another interval
            elif self.killed_containers and random.random() < 0.5:
                container = self.killed_containers.pop()
                try:
                    docker.start_container(container)
                    print(f"[{time.strftime('%X')}] Chaos recovery: {container} is back up!")
                except Exception as e:
                    print(f"Error starting {container}: {e}")


def worker_flow(user_id: str, item_id: str, item_price: int, results: dict, thread_id: int):
    """A single user trying to buy an item."""
    initial_credit = tu.find_user(user_id).get('credit', 0)
    initial_stock = tu.find_item(item_id).get('stock', 0)
    
    # Needs enough credit to buy 1 item
    if initial_credit < item_price or initial_stock < 1:
        results[thread_id] = "skipped"
        return
        
    try:
        # 1. Create Order
        order = tu.create_order(user_id)
        if 'order_id' not in order:
            results[thread_id] = "failed_create_order"
            return
        order_id = order['order_id']

        # 2. Add Item to Order
        add_item_resp = tu.add_item_to_order(order_id, item_id, 1)
        if not tu.status_code_is_success(add_item_resp):
            results[thread_id] = "failed_add_item"
            return
            
        # 3. Checkout (SAGA / 2PC magic happens here)
        checkout_resp = tu.checkout_order(order_id)
        
        if tu.status_code_is_success(checkout_resp.status_code):
            results[thread_id] = "success"
        else:
            results[thread_id] = f"failed_checkout: {checkout_resp.status_code}"
            
    except Exception as e:
        results[thread_id] = f"error: {str(e)}"

class TestFaultTolerance(unittest.TestCase):
    
    def setUp(self):
        # We start with a clean slate (assume docker compose down -v && up -d was run)
        # However, for the test we will just create a specific user and item.
        self.item = tu.create_item(50)
        self.assertTrue('item_id' in self.item, "Failed to create item")
        self.item_id = self.item['item_id']
        
        self.user = tu.create_user()
        self.assertTrue('user_id' in self.user, "Failed to create user")
        self.user_id = self.user['user_id']
        
        # Add 100 stock
        tu.add_stock(self.item_id, 100)
        
        # Add 5000 credit (enough for 100 purchases)
        tu.add_credit_to_user(self.user_id, 5000)
        
    def test_random_chaos_during_workload(self):
        """Spawns threads generating load while Chaos checks containers."""
        
        print("\n--- Starting Fault Tolerance Test ---")
        
        starting_stock = tu.find_item(self.item_id)['stock']
        starting_credit = tu.find_user(self.user_id)['credit']
        item_price = tu.find_item(self.item_id)['price']
        
        print(f"Starting Stock: {starting_stock}, Starting Credit: {starting_credit}, Item Price: {item_price}")

        # Start chaos
        monkey = ChaosMonkey(kill_interval=2)
        monkey.start()

        results = {}
        threads = []
        NUM_WORKERS = 40 # We want 40 concurrent purchase attempts

        # Launch workers
        for i in range(NUM_WORKERS):
            t = threading.Thread(target=worker_flow, args=(self.user_id, self.item_id, item_price, results, i))
            threads.append(t)
            t.start()
            time.sleep(0.1) # Small stagger

        # Wait for all workers to finish
        for t in threads:
            t.join()

        # Stop chaos and recover
        monkey.stop()
        
        # --- VERIFICATION ---
        print("Waiting for async transactions to settle...")
        time.sleep(20)

        ending_stock = tu.find_item(self.item_id)['stock']
        ending_credit = tu.find_user(self.user_id)['credit']
        
        print(f"Ending Stock: {ending_stock}, Ending Credit: {ending_credit}")
        
        # Calculate successful purchases based on what the workers reported
        reported_successes = sum(1 for status in results.values() if status == "success")
        print(f"Reported Successes by Workers: {reported_successes}")
        print(f"Worker Output Distribution: {set(results.values())}")
        
        stock_sold = starting_stock - ending_stock
        credit_spent = starting_credit - ending_credit
        expected_credit_spent = stock_sold * item_price
        
        # Assertion 1: Did we lose money? (Credit spent must equal value of stock sold)
        self.assertEqual(
            credit_spent, 
            expected_credit_spent, 
            f"CONSISTENCY FAILURE: Credit spent ({credit_spent}) does not equal value of stock sold ({expected_credit_spent})"
        )
        
        # Assertion 2: All successes must be reflected in stock
        self.assertGreaterEqual(
            stock_sold, 
            reported_successes,
            "CONSISTENCY FAILURE: Workers reported more successes than stock actually sold"
        )
        
        print("✅ System maintained consistency despite container failures!")

if __name__ == '__main__':
    unittest.main()

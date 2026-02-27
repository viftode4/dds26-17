import subprocess
import time
import struct
import random


def run_command(command: str) -> str:
    """Executes a shell command and returns the output."""
    try:
        result = subprocess.run(
            command, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {command}\nError: {e.stderr}")
        raise


def kill_container(container_name: str):
    """Kills a Docker container forcefully."""
    print(f"💀 Killing container: {container_name}")
    run_command(f"docker kill {container_name}")


def start_container(container_name: str):
    """Starts a previously killed Docker container."""
    print(f"🚀 Starting container: {container_name}")
    run_command(f"docker start {container_name}")


def restart_container(container_name: str):
    """Restarts a Docker container."""
    print(f"🔄 Restarting container: {container_name}")
    run_command(f"docker restart {container_name}")

"""SSH transport layer for communicating with the Junior agent machine."""

from __future__ import annotations

import logging
from pathlib import Path

import paramiko

logger = logging.getLogger("dualmind.ssh")


class SSHBridge:
    """Thin wrapper around paramiko for running commands on the Junior machine."""

    def __init__(self, host: str, user: str, key_path: str):
        self.host = host
        self.user = user
        self.key_path = str(Path(key_path).expanduser())
        self._client: paramiko.SSHClient | None = None

    def connect(self):
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            hostname=self.host,
            username=self.user,
            key_filename=self.key_path,
        )
        logger.info(f"SSH connected to {self.user}@{self.host}")

    def disconnect(self):
        if self._client:
            self._client.close()
            logger.info("SSH disconnected")

    def run(self, command: str, timeout: int = 120) -> tuple[str, str, int]:
        """Run a shell command. Returns (stdout, stderr, exit_code)."""
        if not self._client:
            raise RuntimeError("SSHBridge not connected. Call connect() first.")
        _, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        return stdout.read().decode(), stderr.read().decode(), exit_code

    def upload_file(self, local_path: str, remote_path: str):
        """Upload a file to the remote machine."""
        sftp = self._client.open_sftp()
        sftp.put(local_path, remote_path)
        sftp.close()
        logger.debug(f"Uploaded {local_path} → {self.host}:{remote_path}")

    def download_file(self, remote_path: str, local_path: str):
        """Download a file from the remote machine."""
        sftp = self._client.open_sftp()
        sftp.get(remote_path, local_path)
        sftp.close()
        logger.debug(f"Downloaded {self.host}:{remote_path} → {local_path}")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

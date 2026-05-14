"""SSH transport layer for communicating with the Junior agent machine."""

from __future__ import annotations

import logging
import select
import socketserver
import threading
from pathlib import Path

import paramiko

logger = logging.getLogger("dualmind.ssh")


class SSHBridge:
    """Thin wrapper around paramiko for running commands and tunneling."""

    def __init__(self, host: str, user: str, key_path: str):
        self.host = host
        self.user = user
        self.key_path = str(Path(key_path).expanduser())
        self._client: paramiko.SSHClient | None = None
        self._tunnels: list[threading.Thread] = []

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
            self._client = None
            logger.info("SSH disconnected")

    def run(self, command: str, timeout: int = 120) -> tuple[str, str, int]:
        """Run a shell command. Returns (stdout, stderr, exit_code)."""
        if not self._client:
            raise RuntimeError("SSHBridge not connected. Call connect() first.")
        _, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        return stdout.read().decode(), stderr.read().decode(), exit_code

    def open_tunnel(self, remote_port: int, local_port: int, remote_host: str = "127.0.0.1"):
        """Establish a local port forward: localhost:local_port -> remote_host:remote_port."""
        if not self._client:
            raise RuntimeError("SSHBridge not connected. Call connect() first.")

        transport = self._client.get_transport()

        class ForwardServer(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    chan = transport.open_channel(
                        "direct-tcpip",
                        (remote_host, remote_port),
                        self.request.getpeername(),
                    )
                except Exception as e:
                    logger.error(f"Incoming request to {local_port} failed: {e}")
                    return

                if chan is None:
                    logger.error(f"Incoming request to {local_port} was rejected.")
                    return

                try:
                    while True:
                        r, w, x = select.select([self.request, chan], [], [])
                        if self.request in r:
                            data = self.request.recv(1024)
                            if len(data) == 0:
                                break
                            chan.send(data)
                        if chan in r:
                            data = chan.recv(1024)
                            if len(data) == 0:
                                break
                            self.request.send(data)
                except Exception:
                    pass
                finally:
                    chan.close()
                    self.request.close()

        server = ForwardServer(("127.0.0.1", local_port), Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self._tunnels.append(t)
        logger.info(f"SSH Tunnel: localhost:{local_port} -> {remote_host}:{remote_port}")

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

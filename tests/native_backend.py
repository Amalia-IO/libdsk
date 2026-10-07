import queue
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


class NativeBackend:
    def __init__(self, executable: str, directory: str, timeout: float = 10.0):
        self.executable = Path(executable).resolve(strict=True)
        self.directory = Path(directory).resolve(strict=True)
        self.timeout = timeout
        self.process = None
        self.stderr = None
        self.lock = threading.Lock()

    def _read_exact(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            chunk = self.process.stdout.read(size - len(result))
            if not chunk:
                raise RuntimeError("Native backend returned a truncated response")
            result.extend(chunk)
        return bytes(result)

    def _bounded(self, action):
        result = queue.Queue(maxsize=1)

        def execute():
            try:
                result.put((True, action()))
            except Exception as error:
                result.put((False, error))

        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        try:
            success, value = result.get(timeout=self.timeout)
        except queue.Empty as error:
            self.process.kill()
            self.process.wait()
            worker.join(2)
            raise TimeoutError("Native backend response timed out") from error
        worker.join()
        if not success:
            raise value
        return value

    def start(self):
        if self.process is not None:
            self.stop()
        command = [str(self.executable)]
        if sys.platform == "win32":
            if not (self.executable.parent / "libdsk.dll").is_file():
                raise FileNotFoundError("Native backend requires a sibling libdsk.dll")
            command.append(str(self.executable.parent))
        self.stderr = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(
                command, cwd=self.directory, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.stderr)
            if self._bounded(lambda: self._read_exact(2)) != b"\x00\x00":
                raise RuntimeError("Native backend startup handshake failed")
        except Exception:
            self.stop()
            raise

    def call(self, request: bytes) -> bytes:
        if not 0 < len(request) <= 65535:
            raise ValueError("Invalid native RPC request length")
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self.start()

            def exchange():
                self.process.stdin.write(struct.pack(">H", len(request)) + request)
                self.process.stdin.flush()
                size = struct.unpack(">H", self._read_exact(2))[0]
                return self._read_exact(size)

            return self._bounded(exchange)

    def stop(self):
        if self.process is not None:
            try:
                if self.process.stdin is not None:
                    self.process.stdin.close()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            finally:
                if self.process.stdout is not None:
                    self.process.stdout.close()
                self.process = None
        if self.stderr is not None:
            self.stderr.close()
            self.stderr = None
"""Real TLS loopback protocol tests. No model/GPU, and no plaintext listener."""
import pathlib
import socket
import ssl
import struct
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="strata-stage-rpc-test-")
        cls.path = pathlib.Path(cls.tmp.name)
        cls.exe = cls.path / "fixture"
        subprocess.run(["g++", "-std=c++20", "-Wall", "-Wextra", "-Werror", "-I" + str(ROOT / "include"),
                        str(ROOT / "tests/rpc/transport_fixture.cpp"), str(ROOT / "src/rpc/transport.cpp"),
                        str(ROOT / "src/rpc/client.cpp"), "-lssl", "-lcrypto", "-o", str(cls.exe)], check=True)
        cls.openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", "ca.key", "-out", "ca.crt", "-subj", "/CN=stage-test-ca",
                    "-addext", "basicConstraints=critical,CA:TRUE",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        for role in ("strata-stage-worker", "strata-stage-coordinator", "wrong-role"):
            cls.openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", role + ".key",
                        "-out", role + ".csr", "-subj", "/CN=" + role,
                        "-addext", "subjectAltName=DNS:" + role)
            cls.openssl("x509", "-req", "-in", role + ".csr", "-CA", "ca.crt", "-CAkey", "ca.key",
                        "-CAcreateserial", "-days", "1", "-copy_extensions", "copy", "-out", role + ".crt")

    @classmethod
    def openssl(cls, *args):
        subprocess.run(["openssl", *args], cwd=cls.path, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_pair(self, client_role, server_role="strata-stage-worker", pipeline=False, mode=None):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            endpoint = "127.0.0.1:" + str(s.getsockname()[1])
        server = subprocess.Popen([str(self.exe), mode or ("pipeline-server" if pipeline else "server"), endpoint, str(self.path), server_role],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(0.1)
            client = subprocess.run([str(self.exe), "pipeline-client" if pipeline else "client", endpoint, str(self.path), client_role],
                                    capture_output=True, timeout=15)
            _, error = server.communicate(timeout=10)
            return client, server.returncode, error
        finally:
            if server.poll() is None:
                server.kill()
                server.communicate()

    def test_segmented_fp32_payload_and_epoch_envelope(self):
        client, code, error = self.run_pair("strata-stage-coordinator")
        self.assertEqual(client.returncode, 0, client.stderr)
        self.assertEqual(code, 0, error)
        self.assertIn(b'"calls":3', client.stdout)

    def test_wrong_client_role_rejected(self):
        client, code, error = self.run_pair("wrong-role")
        self.assertNotEqual(client.returncode, 0)
        self.assertNotEqual(code, 0)
        self.assertIn(b"certificate identity", error)

    def test_wrong_server_identity_rejected(self):
        client, code, _ = self.run_pair("strata-stage-coordinator", "wrong-role")
        self.assertNotEqual(client.returncode, 0)
        self.assertNotEqual(code, 0)

    def test_two_chunk_pipeline_cancellation_rewinds_worker(self):
        client, code, error = self.run_pair("strata-stage-coordinator", pipeline=True)
        self.assertEqual(client.returncode, 0, client.stderr)
        self.assertEqual(code, 0, error)

    def test_stale_sequence_invalidates_client(self):
        client, _, _ = self.run_pair("strata-stage-coordinator", mode="stale-server")
        self.assertNotEqual(client.returncode, 0)
        self.assertIn(b"response epoch/sequence/window mismatch", client.stderr)

    def test_worker_disconnect_is_terminal(self):
        client, code, _ = self.run_pair("strata-stage-coordinator", mode="disconnect-server")
        self.assertEqual(code, 0)
        self.assertNotEqual(client.returncode, 0)
        self.assertIn(b"both stage states are invalid", client.stderr)

    def test_malformed_or_oversized_frame_rejected_before_allocation(self):
        for magic, op, size in ((0, 1, 0), (0x3150435241545253, 99, 0), (0x3150435241545253, 1, (512 << 20) + 1)):
            with self.subTest(magic=magic, op=op, size=size), socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
                listener.close()
                endpoint = "127.0.0.1:" + str(port)
                server = subprocess.Popen([str(self.exe), "server", endpoint, str(self.path), "strata-stage-worker"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    time.sleep(0.1)
                    context = ssl.create_default_context(cafile=str(self.path / "ca.crt"))
                    context.load_cert_chain(str(self.path / "strata-stage-coordinator.crt"),
                                            str(self.path / "strata-stage-coordinator.key"))
                    with context.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=5),
                                             server_hostname="strata-stage-worker") as stream:
                        stream.sendall(struct.pack("<6Q", magic, op, 1, 1, 0, size))
                    _, error = server.communicate(timeout=10)
                    self.assertNotEqual(server.returncode, 0)
                    self.assertIn(b"incompatible or oversized frame", error)
                finally:
                    if server.poll() is None:
                        server.kill()
                    server.communicate()


if __name__ == "__main__":
    unittest.main()

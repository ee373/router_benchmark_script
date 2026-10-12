#!/usr/bin/env python3
"""DS-Lite source-port scan checks that do not require benchmark hardware."""

import socket
import threading
import unittest

from tests.run_router_bench_dslite_tcp_source_port_scan import (
    REQUEST,
    REQUEST_MAGIC,
    RESPONSE,
    RESPONSE_MAGIC,
    build_scan_ports,
    classify_port_results,
    tcp_probe,
)


class DsliteSourcePortScanTest(unittest.TestCase):
    def test_build_scan_ports_is_descending_and_inclusive(self):
        self.assertEqual(build_scan_ports(5, 1, 1), [5, 4, 3, 2, 1])
        self.assertEqual(build_scan_ports(10, 4, 2), [10, 8, 6, 4])

    def test_classification_and_range_compression(self):
        rows = [
            {"source_port": 100, "success": 1, "result": "success"},
            {"source_port": 101, "success": 1, "result": "success"},
            {"source_port": 102, "success": 0, "result": "connect_timeout"},
            {"source_port": 103, "success": 0, "result": "connect_timeout"},
            {"source_port": 104, "success": 1, "result": "success"},
            {"source_port": 104, "success": 0, "result": "connect_timeout"},
        ]
        per_port, ranges, boundaries = classify_port_results(rows)
        self.assertEqual([item["classification"] for item in per_port], [
            "usable", "usable", "unusable", "unusable", "intermittent"
        ])
        self.assertEqual((ranges[0]["low_port"], ranges[0]["high_port"]), (100, 101))
        self.assertEqual((ranges[1]["low_port"], ranges[1]["high_port"]), (102, 103))
        self.assertEqual(len(boundaries), 2)

    def test_tcp_probe_verifies_round_trip(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        server_port = listener.getsockname()[1]

        def server():
            conn, _address = listener.accept()
            with conn:
                request = bytearray()
                while len(request) < REQUEST.size:
                    request.extend(conn.recv(REQUEST.size - len(request)))
                magic, source_port, scan_index = REQUEST.unpack(bytes(request))
                self.assertEqual(magic, REQUEST_MAGIC)
                conn.sendall(RESPONSE.pack(RESPONSE_MAGIC, source_port, scan_index))
            listener.close()

        thread = threading.Thread(target=server)
        thread.start()
        row = tcp_probe(
            client_ip="127.0.0.1",
            source_port=0,
            server_ip="127.0.0.1",
            server_port=server_port,
            connect_timeout_sec=1.0,
            io_timeout_sec=1.0,
            repeat=1,
            scan_index=1,
        )
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(row["result"], "success")
        self.assertEqual(row["success"], 1)
        self.assertGreater(row["local_port"], 0)


if __name__ == "__main__":
    unittest.main()

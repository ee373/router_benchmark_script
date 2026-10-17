import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.lib import arp, ndp
from tests.lib.refresh import EndpointRefresher


class RefreshLifecycleTests(unittest.TestCase):
    def test_success_and_endpoint_failure_are_logged_and_required_is_enforced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "details" / "refresh.csv"

            def refresh(endpoint):
                if endpoint["namespace"] == "wan":
                    raise ValueError("unreachable")
                return "resolved"

            refresher = EndpointRefresher(
                [
                    {"namespace": "lan", "iface": "eth0", "src_ip": "192.0.2.2", "target_ip": "192.0.2.1"},
                    {"namespace": "wan", "iface": "eth1", "src_ip": "198.51.100.2", "target_ip": "198.51.100.1"},
                ],
                log_path,
                60,
                refresh,
                "WARNING: refresh failed",
                "test-refresh",
            )

            with mock.patch("builtins.print") as print_mock:
                self.assertFalse(refresher.refresh_now(reason="manual", required=False))
            self.assertEqual(print_mock.call_count, 1)
            with self.assertRaisesRegex(RuntimeError, "unreachable"):
                refresher.refresh_now(reason="required", required=True)

            with log_path.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(
                rows[0],
                ["ts_local", "reason", "namespace", "iface", "src_ip", "target_ip", "result", "detail"],
            )
            self.assertEqual(len(rows), 5)
            self.assertEqual(rows[1][1:], ["manual", "lan", "eth0", "192.0.2.2", "192.0.2.1", "ok", "resolved"])
            self.assertEqual(rows[2][1:], ["manual", "wan", "eth1", "198.51.100.2", "198.51.100.1", "error", "unreachable"])
            self.assertEqual(rows[3][1], "required")

    def test_worker_uses_existing_periodic_reason(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            refresher = EndpointRefresher([], Path(temp_dir) / "refresh.csv", 10, lambda _e: None, "WARNING", "worker")
            refresher._stop.wait = mock.Mock(side_effect=[False, True])
            refresher.refresh_now = mock.Mock()

            refresher._worker()

            refresher.refresh_now.assert_called_once_with(reason="periodic_60s", required=False)

    def test_start_is_idempotent_and_stop_signals_and_joins_worker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch("tests.lib.refresh.threading.Thread") as thread_factory:
                thread = thread_factory.return_value
                refresher = EndpointRefresher([], Path(temp_dir) / "refresh.csv", 3, lambda _e: None, "WARNING", "worker")

                refresher.start()
                refresher.start()
                refresher.stop()

            thread_factory.assert_called_once_with(
                target=refresher._worker,
                name="worker",
                daemon=True,
            )
            thread.start.assert_called_once_with()
            thread.join.assert_called_once_with(timeout=4.0)
            self.assertTrue(refresher._stop.is_set())
            self.assertIsNone(refresher._thread)

    def test_arp_and_ndp_wrappers_keep_protocol_specific_callbacks(self):
        endpoint = {
            "namespace": "bench",
            "iface": "eth0",
            "src_ip": "192.0.2.2",
            "target_ip": "192.0.2.1",
            "count": 4,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            arp_refresher = arp.NamespaceArpRefresher(
                [endpoint],
                Path(temp_dir) / "arp.csv",
            )
            with mock.patch("tests.lib.arp.send_namespace_arp") as send_arp:
                self.assertTrue(arp_refresher.refresh_now())
            send_arp.assert_called_once_with("bench", "eth0", "192.0.2.2", "192.0.2.1", count=4)

            ndp_endpoint = dict(endpoint, src_ip="2001:db8::2", target_ip="2001:db8::1")
            ndp_refresher = ndp.NamespaceNdpRefresher(
                [ndp_endpoint],
                Path(temp_dir) / "ndp.csv",
            )
            with mock.patch(
                "tests.lib.ndp.refresh_namespace_neighbor",
                return_value="lladdr 02:00:00:00:00:01 REACHABLE",
            ) as refresh_neighbor:
                self.assertTrue(ndp_refresher.refresh_now())
                ndp_refresher.start()
                ndp_refresher.stop()
            refresh_neighbor.assert_called_once_with(
                "bench",
                "eth0",
                "2001:db8::2",
                "2001:db8::1",
            )


if __name__ == "__main__":
    unittest.main()

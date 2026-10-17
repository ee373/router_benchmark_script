import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.lib import network, pci, trex


class PciHelperTests(unittest.TestCase):
    def test_bdf_normalization_preserves_network_and_trex_contracts(self):
        self.assertEqual(pci.normalize_pci_bdf("07:0A.0"), "0000:07:0a.0")
        self.assertEqual(
            pci.normalize_pci_bdf("ABCD:07:0A.7"),
            "abcd:07:0a.7",
        )
        self.assertEqual(pci.normalize_pci_bdf("bad"), "")
        self.assertEqual(pci.normalize_pci_bdf(""), "")
        self.assertEqual(network.normalize_pci_bdf("bad"), "")
        self.assertEqual(trex.normalize_pci_bdf("not:a-bdf"), "0000:not:a-bdf")
        self.assertEqual(trex.normalize_pci_bdf("not-a-bdf"), "not-a-bdf")
        self.assertEqual(trex.normalize_pci_bdf(""), "")

    def test_device_path_helpers_keep_caller_specific_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(pci, "PCI_SYSFS_ROOT", Path(temp_dir)):
                self.assertEqual(
                    network.pci_device_path("07:00.0"),
                    Path(temp_dir) / "devices" / "0000:07:00.0",
                )
                self.assertIsNone(network.pci_device_path("invalid"))
                self.assertEqual(
                    trex.pci_device_path("07:00.0"),
                    Path(temp_dir) / "devices" / "0000:07:00.0",
                )
                self.assertEqual(
                    trex.pci_device_path("invalid"),
                    Path(temp_dir) / "devices" / "invalid",
                )

    def test_current_driver_reads_link_and_returns_empty_when_unbound(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            device_path = Path(temp_dir) / "device"
            device_path.mkdir()
            self.assertEqual(pci.current_driver_from_device_path(device_path), "")

            driver_path = Path(temp_dir) / "drivers" / "ixgbe"
            driver_path.mkdir(parents=True)
            (device_path / "driver").symlink_to(driver_path, target_is_directory=True)
            self.assertEqual(pci.current_driver_from_device_path(device_path), "ixgbe")

    def test_current_driver_returns_empty_after_sysfs_access_errors(self):
        with mock.patch.object(Path, "exists", return_value=True), mock.patch.object(
            Path,
            "is_symlink",
            return_value=True,
        ), mock.patch(
            "tests.lib.pci.os.readlink",
            side_effect=OSError("sysfs unavailable"),
        ), mock.patch.object(
            Path,
            "resolve",
            side_effect=OSError("sysfs unavailable"),
        ):
            self.assertEqual(pci.current_driver_from_device_path("/sys/device"), "")

    def test_network_and_trex_keep_unbind_error_contracts(self):
        class Writer:
            def __init__(self):
                self.rows = []

            def writerow(self, row):
                self.rows.append(row)

        network_writer = Writer()
        trex_writer = Writer()
        self.assertFalse(
            network.pci_unbind_current_driver(
                "invalid",
                "ixgbe",
                log_writer=network_writer,
                required=True,
            )
        )
        self.assertEqual(
            network_writer.rows[0][1],
            "pci_unbind_skip_no_current_driver",
        )
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            pci,
            "PCI_SYSFS_ROOT",
            Path(temp_dir),
        ):
            with self.assertRaises(SystemExit):
                network.pci_unbind_current_driver(
                    "07:00.0",
                    "ixgbe",
                    log_writer=network_writer,
                    required=True,
                )
            with self.assertRaises(RuntimeError):
                trex.pci_unbind_current_driver(
                    "07:00.0",
                    "ixgbe",
                    writer=trex_writer,
                    required=True,
                )
        self.assertEqual(network_writer.rows[-1][1], "pci_unbind_warning")
        self.assertEqual(trex_writer.rows[-1][1], "pci_unbind_warning")

    def test_network_and_trex_unbind_share_candidate_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            device = root / "devices" / "0000:07:00.0"
            unbind_path = device / "driver" / "unbind"
            unbind_path.parent.mkdir(parents=True)
            unbind_path.write_text("", encoding="utf-8")
            with mock.patch.object(pci, "PCI_SYSFS_ROOT", root), mock.patch(
                "tests.lib.network.sysfs_write"
            ) as network_write, mock.patch("tests.lib.trex.sysfs_write") as trex_write:
                self.assertTrue(
                    network.pci_unbind_current_driver("07:00.0", "ixgbe")
                )
                self.assertTrue(trex.pci_unbind_current_driver("07:00.0", "ixgbe"))

            network_write.assert_called_once_with(unbind_path, "0000:07:00.0")
            trex_write.assert_called_once_with(unbind_path, "0000:07:00.0")


if __name__ == "__main__":
    unittest.main()

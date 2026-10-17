"""Shared endpoint logging and periodic worker lifecycle for ARP/NDP refreshers."""

import csv
import threading
from pathlib import Path

from .common import ts_iso


_REFRESH_LOG_COLUMNS = [
    "ts_local",
    "reason",
    "namespace",
    "iface",
    "src_ip",
    "target_ip",
    "result",
    "detail",
]


class EndpointRefresher:
    def __init__(
        self,
        endpoints,
        log_path,
        interval_sec,
        refresh_endpoint,
        warning_prefix,
        thread_name,
    ):
        self.endpoints = [dict(endpoint) for endpoint in endpoints if endpoint]
        self.log_path = Path(log_path)
        self.interval_sec = max(1.0, float(interval_sec))
        self.refresh_endpoint = refresh_endpoint
        self.warning_prefix = warning_prefix
        self.thread_name = thread_name
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            with self.log_path.open("w", newline="", encoding="utf-8") as stream:
                csv.writer(stream).writerow(_REFRESH_LOG_COLUMNS)

    def _log(self, reason, endpoint, result, detail=""):
        with self._lock:
            with self.log_path.open("a", newline="", encoding="utf-8") as stream:
                csv.writer(stream).writerow([
                    ts_iso(),
                    reason,
                    endpoint.get("namespace", "root"),
                    endpoint.get("iface", ""),
                    endpoint.get("src_ip", ""),
                    endpoint.get("target_ip", ""),
                    result,
                    detail,
                ])

    def refresh_now(self, reason="manual", required=False):
        failures = []
        for endpoint in self.endpoints:
            try:
                detail = self.refresh_endpoint(endpoint)
                self._log(reason, endpoint, "ok", detail or "")
            except Exception as exc:
                failures.append(str(exc))
                self._log(reason, endpoint, "error", str(exc))
                print(f"{self.warning_prefix}: {exc}", flush=True)
        if failures and required:
            raise RuntimeError("; ".join(failures))
        return not failures

    def _worker(self):
        while not self._stop.wait(self.interval_sec):
            self.refresh_now(reason="periodic_60s", required=False)

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._worker,
            name=self.thread_name,
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=min(5.0, self.interval_sec + 1.0))
            self._thread = None

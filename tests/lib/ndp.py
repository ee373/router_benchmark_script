#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Periodic IPv6 neighbor discovery refresh for benchmark namespaces."""

import subprocess

from .config import load_defaults as _load_defaults
from .refresh import EndpointRefresher


DEFAULT_NDP_REFRESH_INTERVAL_SEC = float(
    _load_defaults("common")["arp_refresh_interval_sec"]
)


def refresh_namespace_neighbor(namespace, iface, src_ip, target_ip, timeout_sec=8.0):
    prefix = [] if not namespace or namespace == "root" else [
        "ip", "netns", "exec", str(namespace)
    ]
    ping_cmd = prefix + [
        "ping", "-6", "-c", "1", "-W", "1", "-I", str(iface), str(target_ip)
    ]
    subprocess.run(
        ping_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=float(timeout_sec),
        check=False,
    )
    neigh_cmd = prefix + [
        "ip", "-6", "neigh", "show", "to", str(target_ip), "dev", str(iface)
    ]
    proc = subprocess.run(
        neigh_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=float(timeout_sec),
        check=False,
    )
    state = (proc.stdout or "").strip()
    if (
        proc.returncode != 0
        or "lladdr" not in state
        or "FAILED" in state
        or "INCOMPLETE" in state
    ):
        raise RuntimeError(
            f"IPv6 neighbor resolution failed: ns={namespace} iface={iface} "
            f"src={src_ip} target={target_ip} state={state or proc.stderr.strip()}"
        )
    return state


class NamespaceNdpRefresher:
    def __init__(self, endpoints, log_path, interval_sec=DEFAULT_NDP_REFRESH_INTERVAL_SEC):
        self._lifecycle = EndpointRefresher(
            endpoints,
            log_path,
            interval_sec,
            self._refresh_endpoint,
            "WARNING: IPv6 neighbor refresh failed",
            "routerbench-ndp-refresh",
        )

    @property
    def endpoints(self):
        return self._lifecycle.endpoints

    @property
    def log_path(self):
        return self._lifecycle.log_path

    @property
    def interval_sec(self):
        return self._lifecycle.interval_sec

    def _log(self, reason, endpoint, result, detail=""):
        self._lifecycle._log(reason, endpoint, result, detail)

    def _refresh_endpoint(self, endpoint):
        return refresh_namespace_neighbor(
            endpoint.get("namespace", "root"),
            endpoint["iface"],
            endpoint["src_ip"],
            endpoint["target_ip"],
        )

    def refresh_now(self, reason="manual", required=False):
        return self._lifecycle.refresh_now(reason=reason, required=required)

    def _worker(self):
        self._lifecycle._worker()

    def start(self):
        self._lifecycle.start()

    def stop(self):
        self._lifecycle.stop()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()

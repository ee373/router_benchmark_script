# Benchmark configuration

The benchmark scripts read their built-in defaults from JSON files in this directory.

| File | Contents |
|---|---|
| `benchmark_defaults.json` | Launcher order, TRex paths, measurement durations, thresholds, rates, ports, namespace defaults, and other runtime defaults. |
| `target_profiles.json` | IPv4 NAT NIC, PCI, driver, address, line-rate, and TRex-core profiles. |
| `target_dslite_profiles.json` | DS-Lite NIC, WAN link/ARP, server, AFTR NAT pool, inner MTU, tunnel overhead, and TRex-core profiles. |
| `target_mape_profiles.json` | MAP-E NIC, rule, PSID, shared IPv4, BR, MTU, external server, and TRex-core profiles. |

Command-line options still override the configured defaults. `TREX_RESULT_ROOT`,
`TREX_DPDK_DRIVER`, and `ROUTERBENCH_TEST_START_WAIT_SEC` retain their existing
environment-variable behavior.

Protocol constants, packet formats, and CSV column definitions remain in Python
because they define file or wire formats rather than operator-adjustable defaults.

`target_profiles.json` supports an `extends` field. The derived profile inherits
the named profile and then replaces the fields declared in the derived object.
`target_dslite_profiles.json` and `target_mape_profiles.json` use the same
inheritance behavior.

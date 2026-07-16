# Launcher test selection

`test_basic_speed.py` uses one selection rule set:

- `--run-all`: run the standard full set, including TCP scratch max sessions, ASTF NAT stability, and the delayed DNS table pass. Legacy TRex ASTF max-session/CPS tests are not included.
- No `--run-*` option: run the default set, including TCP scratch max sessions and excluding ASTF NAT stability.
- One or more individual `--run-*` options: run only the specified tests, in launcher order.

Implemented individual selectors:

- `--run-stl-throughput`: IPv4 STL throughput/PPS.
- `--run-tcp-scratch-max-sessions`: IPv4 TCP scratch maximum session test without TRex. This is included in the default set and `--run-all`. The scratch test prints persistent progress lines every 10 seconds by default, accepts `--progress-interval-sec` to change that interval, accepts `--session-cps` or `--session-ramp-cps` for connection creation CPS, and treats an active session as disconnected when a keepalive reply is not received within `--keepalive-timeout-sec` seconds. The default timeout is `1.0` second.
- `--run-astf-max-sessions`: legacy TRex ASTF maximum session test. This runs only when this selector is specified.
- `--run-astf-sessions-cps`: legacy TRex ASTF short-session CPS test. This runs only when this selector is specified.
- `--run-astf-natstability`: IPv4 ASTF NAT stability soak.
- `--run-l4-latency`: IPv4 L4 request-response latency at 0%, 10%, 20%, 30%, 40%, 50%, 60%, 70%, 80%, 90%, 95%, and 99% load. The default L4 transport is UDP for both latency and iperf3 load. The 0% phase is recorded as `idle`.

Default example:

```bash
sudo python3 test_basic_speed.py \
  --target i226_2p5g \
  --duration 30
```

All tests:

```bash
sudo python3 test_basic_speed.py \
  --run-all \
  --target i226_2p5g \
  --duration 30
```

Selected tests only:

```bash
sudo python3 test_basic_speed.py \
  --run-stl-throughput \
  --run-l4-latency \
  --target i226_2p5g \
  --duration 30
```

For `x540_10g`, STL uses two DP cores while ASTF tests use one DP core by default. The ASTF profile uses one client and one server IP address, matching the working i226 configuration. An explicit `--cores` value overrides both defaults.

NAT stability duration is controlled with `--soak-duration-sec`:

```bash
sudo python3 test_basic_speed.py \
  --run-astf-natstability \
  --target i226_2p5g \
  --soak-duration-sec 3600
```

## L4 latency interface names

`--run-l4-latency` normally reads the selected target profile.  Fixed Linux interface names can be set in `tests/target_profiles.py` with `lan_iface` and `wan_iface`.  Keep the PCI BDF `interfaces` entries as well; they are used when the NIC must be bound back to the kernel driver after TRex/DPDK tests.

## Invalid options

Unknown or not-applicable options are rejected by the launcher before any benchmark starts. This applies to all long options, not only `--run-*` selectors.

The launcher accepts child-script options only when at least one selected test supports that option. For example, `--max-cps` is rejected when only `--run-l4-latency` is selected, and `--l4-transport` is rejected when only `--run-stl-throughput` is selected. This prevents typos and prevents options for a different benchmark from being silently ignored.

For example, this selector typo is invalid and exits before measurement:

```bash
sudo python3 test_basic_speed.py \
  --target i226_1g \
  --run-astfnat-stability
```

Use the exact selector name instead:

```bash
sudo python3 test_basic_speed.py \
  --target i226_1g \
  --run-astf-natstability
```

## Direct L2 loop baseline target profiles

The following targets are direct-cable baseline profiles for L4 latency measurement:

- `i226_1g_loop`
- `i226_2p5g_loop`
- `x540_10g_loop`

They are intended for `--run-l4-latency` only. Example:

```bash
sudo python3 test_basic_speed.py \
  --run-l4-latency \
  --target i226_1g_loop \
  --duration 30
```

The launcher rejects STL/ASTF tests with these loop profiles because no router/DUT is present.

## Strict option examples

Valid L4-only execution:

```bash
sudo python3 test_basic_speed.py \
  --run-l4-latency \
  --target i226_2p5g \
  --duration 30 \
  --l4-transport udp
```

Invalid because `--max-cps` is not used by the selected L4 latency test:

```bash
sudo python3 test_basic_speed.py \
  --run-l4-latency \
  --target i226_2p5g \
  --max-cps 100
```

Invalid because `--run-all` and individual selectors are mutually exclusive:

```bash
sudo python3 test_basic_speed.py \
  --run-all \
  --run-l4-latency \
  --target i226_2p5g
```

`--soak-duration-sec` is valid only when ASTF NAT stability is selected with `--run-astf-natstability` or `--run-all`.

## TRex/L4 NIC state transition

STL/ASTF tests and L4 latency tests require different NIC driver states:

- STL/ASTF: PCI devices must be bound to the TRex/DPDK driver.
- L4 latency: Linux netdevs must exist, so PCI devices must be bound to the kernel driver and then moved into `rb_lan` / `rb_wan` namespaces.

The scripts now handle this transition automatically:

- Before STL/ASTF starts, stale `rb_lan` / `rb_wan` namespaces are removed and profile PCI devices are bound to `dpdk_drivers`.
- Before L4 latency starts, profile PCI devices are bound to `kernel_drivers`.
- After L4 latency ends, namespaces are removed and profile PCI devices are bound back to `dpdk_drivers` by default.

`tests/target_profiles.py` must define both driver sets. Example:

```python
"i226_1g": {
    "interfaces": ["05:00.0", "04:00.0"],
    "kernel_drivers": ["igc", "igc"],
    "dpdk_drivers": ["vfio-pci", "vfio-pci"],
    "lan_iface": "enp5s0",
    "wan_iface": "enp4s0",
    ...
}
```

If the host uses `igb_uio` instead of `vfio-pci`, either update `dpdk_drivers` in the profile or pass:

```bash
--dpdk-driver igb_uio
```

For diagnostics, each TRex test writes:

```text
trex_interface_prepare.csv
```

This file records namespace deletion and PCI driver binding actions before TRex startup.


## NIC driver state handling

Before TRex-based tests, the launcher-side test scripts remove stale `rb_lan` / `rb_wan` namespaces and prepare the PCI devices for TRex/DPDK.  The PCI driver detection now treats an absent sysfs `driver` symlink as unbound; it no longer accepts the synthetic string `driver` as a real driver name.  Unbind operations are best-effort and the final driver state is validated after bind.  This prevents a stale namespace or a transient unbound PCI state from aborting the benchmark before TRex starts.

## TRex/DPDK driver recovery note

The launcher now treats `dpdk_drivers: ["auto", "auto"]` as the default for the
built-in profiles.  Before STL/ASTF, stale benchmark namespaces are removed and
profile PCI devices are bound to a TRex/DPDK driver.  Auto mode first keeps an
already active DPDK driver (`vfio-pci`, `igb_uio`, or `uio_pci_generic`), then
tries `TREX_DPDK_DRIVER`, `igb_uio`, `vfio-pci`, and `uio_pci_generic`.

If the lab host requires a fixed driver, use one of the following:

```bash
sudo TREX_DPDK_DRIVER=igb_uio python3 test_basic_speed.py --target i226_1g --duration 60 --run-all
sudo python3 test_basic_speed.py --target i226_1g --duration 60 --run-all --dpdk-driver igb_uio
```


## DNS rate/cache/table benchmark

The launcher includes `--run-dns-latency`. In the default test set it runs after L4 latency and writes results under `05_dns_forwarder_cache_latency`.
DNS delayed table pass: `--run-dns-latency` does not run the 600-second delayed `cache_table_size` pass. Use `--run-dns-all` for DNS-only execution including that delayed pass, or `--run-all` for the complete benchmark including the delayed DNS pass.


The DNS test starts a built-in dummy UDP DNS server on the benchmark host WAN namespace and sends DNS A queries from the LAN namespace to the router/DUT DNS forwarder. Configure the router/DUT WAN DNS server to the benchmark host WAN IP before running this test. The generated query name suffix defaults to `routerbench.example.net`.

The current DNS test contains three subtests:

```text
nocache_rate:
  Unique qnames at E6 QPS steps from 10 to 10000.
  Measures latency and success rate.
  Stops when failure_rate_pct >= 50 or rtt_max_ms >= 1000.

cache_rate:
  Preloads 100 qnames.
  Reuses those 100 qnames in round-robin order at cache E6 QPS steps from 10 to 48000.
  Measures latency and success rate.
  Stops when failure_rate_pct >= 50 or rtt_max_ms >= 1000.

cache_table_reuse_size:
  Starts from the qnames preloaded by cache_rate.
  Each E6 step adds only the missing qnames and verifies the accumulated pool at 100 QPS.
  Stops when failure_rate_pct >= 50 or wan_queries_delta / cache_table_size >= 50%.

cache_table_size:
  Runs after a 600 second interval by default.
  Uses an accumulated qname pool instead of a fresh table at every step.
  Stops when failure_rate_pct >= 50 or wan_queries_delta / cache_table_size >= 50%.
```

DNS-only execution:

```bash
sudo python3 test_basic_speed.py   --target i226_1g   --run-dns-latency   --duration 60
```

Short smoke test:

```bash
sudo python3 test_basic_speed.py   --target i226_1g   --run-dns-latency   --duration 3   --dns-e6-steps 10,100   --dns-skip-table
```

See `tests/README.md` for details on all test scripts.

## DHCPv4 basic latency using scratch raw socket

DHCP is implemented by `tests/run_router_bench_v4_dhcp_scratch.py`. TRex EMU is not used. The script now creates a small raw-socket DHCPv4 client from scratch. It sends DHCP DISCOVER/REQUEST/RELEASE frames with forged client MAC addresses from the DUT LAN-side interface inside a Linux network namespace.

Default behavior:

```text
single_client_latency: 1 client x 3 trials
burst_clients_latency: 10 clients x 1 trial
reconnect: same MAC initial lease -> DHCP RELEASE -> 60 sec wait -> same MAC reconnect x 1 trial
```

Execution:

```bash
sudo python3 test_basic_speed.py \
  --target i226_1g \
  --run-dhcp-scratch
```

The historical selector also works and starts the same scratch implementation:

```bash
sudo python3 test_basic_speed.py \
  --target i226_1g \
  --run-dhcp-scratch
```

Main options:

```text
--dhcp-single-repeats
--dhcp-burst-clients
--dhcp-burst-repeats
--dhcp-reconnect-repeats
--dhcp-reconnect-wait-sec
--dhcp-timeout-sec
--dhcp-poll-interval-sec
--dhcp-between-trials-sec
--dhcp-client-mac-base
--dhcp-vlan
--dhcp-qinq
--dhcp-hostname-prefix
--dhcp-release-at-end / --no-dhcp-release-at-end
--dhcp-ns
--lan-iface
--no-auto-kernel-bind
--restore-dpdk-after / --no-restore-dpdk-after
--dpdk-driver
```

Output directory:

```text
06_dhcp_scratch_lease_acquisition
```

Output files:

```text
summary_all.csv
dhcp_scratch_trials.csv
dhcp_scratch_reconnect.csv
dhcp_scratch_clients.csv
dhcp_scratch_events.csv
dhcp_emu_trials.csv          # compatibility alias
dhcp_emu_reconnect.csv       # compatibility alias
dhcp_emu_clients.csv         # compatibility alias
dhcp_emu_events.csv          # compatibility alias
namespace_setup.csv
metadata.json
```

This test is part of the no-option default set. The result can still depend on the DUT lease table and pool size.


DNS note: the DNS benchmark returns deterministic random public-looking A records by default. This avoids common DNS rebinding protection that drops private-address DNS answers. Use `--dns-answer-mode fixed --dns-answer-base-ip 93.184.0.1` if a fixed answer address is needed.

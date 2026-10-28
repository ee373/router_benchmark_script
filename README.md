## 通信事前確認の適用範囲

STL throughput/PPSは、各測定点で事前確認を1回実行してから測定する。
`--stl-recovery-probe`を指定すると、従来どおり事前確認が3回連続で成功するまで
再試行する。その他のテストモードの連続事前確認と、テスト間のRecoveryは
従来どおり動作する。連続確認では失敗時に成功数を0へ戻し、標準の試行間隔は
2秒、復旧待機上限は300秒とする。STL系の再試行結果は`stl_recovery.csv`、
その他のテストモードは各`*_preflight_recovery.csv`、テスト間Recoveryは
`inter_test_recovery.csv`へ保存する。

| 試験 | 適用する確認 | 適用しない項目と理由 |
|---|---|---|
| STL throughput/PPS | ステップごとのARP、明示MAC、NAT外部UDPポート取得、warm NAT成功判定、上り・下り32パケット確認、方向別統計 | なし |
| MAP-E STL throughput/PPS | ステップごとのARP、共有IPv4・PSIDポート取得、上り・下り32パケット確認、方向別統計 | 外側IPv6はVPPが処理するため計測器では生成・取得しない |
| ASTF max sessions / CPS | 各測定前の小規模TCP往復事前試験。接続成功と両方向L7バイトを確認 | NAT外部ポートと明示MACはASTF/TRexが動的に管理するため対象外 |
| ASTF NAT stability | 各サイクル前に同一プロトコル・同一payload directionの小規模往復事前試験 | NAT外部ポートと明示MACはASTF/TRexが動的に管理するため対象外 |
| TCP scratch max sessions | 本測定前にTCP接続でUP要求とDOWN応答を確認。DS-Lite版は同一送信元ポートから異なる2宛先への同時接続も確認 | カーネルTCP/IPがNATポートとEthernetヘッダーを管理するため外部ポート取得・明示MACは対象外 |
| DS-Lite TCP source-port scan | 自動送信元ポートによるTCP往復事前確認後、指定範囲の各送信元ポートでTCP接続とバイナリ要求・応答を確認 | 診断専用のPythonソケット試験であり、TRex、iperf3、外部ポート取得は使用しない |
| L4 latency | 各フェーズ前の既存TCP/UDP request-response preflight | カーネルソケット試験のため外部ポート取得・明示MACは対象外 |
| DNS | 各負荷ステップ前に、固有qnameを使用した3クエリの要求・応答確認を3回連続で実行。事前クエリ分はWAN問い合わせ数から除外 | DNS forwarder試験であり、固定NAT外部UDPポートを生成しない |
| DHCP scratch | 各測定試行前に1クライアントのDISCOVER/OFFER/REQUEST/ACKを3回連続で確認。事前確認用リースはRELEASEする | NAT、warm NAT、双方向負荷統計は対象外 |
| Native IPv6 STL throughput/PPS | ステップごとのNDP、明示MAC、上り・下り32パケット確認、方向別統計 | IPv6ルーティング試験のためNAT処理は対象外 |
| Native IPv6 routing stability | 各サイクル前のNDPと、同一プロトコル・同一payload directionの小規模往復事前試験 | IPv6ルーティング試験のためNAT処理は対象外 |

# MAP-E benchmark launcher

MAP-E measurements use `test_mape_speed.py` and
`tests/config/target_mape_profiles.json`. They do not use
`test_basic_speed.py`. The launcher includes throughput and small-packet
forwarding tests plus a TCP scratch maximum-session benchmark.
The maximum-session test is selected explicitly with
`--run-tcp-scratch-max-sessions` or included by `--run-all`.

```bash
sudo python3 test_mape_speed.py \
  --target i226_1g_mape \
  --duration 20 \
  --repeat 1
```

TRex uses IPv4 on both measurement ports. The WAN port is
`198.51.100.254/24` and sends traffic to VPP at `198.51.100.1`. VPP performs
MAP-E encapsulation and decapsulation. The measurement script therefore does
not set the CE MAP outer IPv6 address and does not substitute the DHCPv6 IA_NA
address for it.

Before every load step, the script creates an uplink mapping and captures the
decapsulated WAN-side IPv4 packet. It requires source IPv4 `198.18.0.12` and a
translated UDP source port in PSID 3's 240-port set. The captured port is then
used as the downlink destination port. The 40-byte IPv6 encapsulation overhead
is included in the outer-line rate estimate.

# DS-Lite benchmark launcher

DS-Lite measurements use a separate launcher and target-profile file. They do
not use `test_basic_speed.py` or `tests/config/target_profiles.json`.

```bash
sudo python3 test_dslite_speed.py \
  --target i226_1g_dslite \
  --duration 20 \
  --repeat 1
```

The DS-Lite test plan contains:

- 1478-byte throughput in downlink, uplink, and bidirectional directions.
- 64-byte packet forwarding in downlink, uplink, and bidirectional directions.
- Per-step discovery of both the AFTR NAT external IPv4 address and UDP port.
- `inner_ipv4_bps` and `outer_line_estimated_bps`, including the configured
  40-byte IPv6 tunnel overhead.
- Maximum concurrent TCP sessions using 1000 unique source ports and 68 WAN
  server IPv4 addresses. The default plan creates 68,000 sessions while VPP
  AFTR keeps at most 1000 endpoint-independent B4 mappings.
- Short-lived TCP session generation with the same ASTF CPS steps, stability
  thresholds, and bidirectional L7 preflight used by the IPv4 NAT benchmark.
- TCP/UDP ASTF NAT stability cycles with the same payload, preflight,
  completion, stability, and output rules used by the IPv4 NAT benchmark.
- TCP/UDP request-response latency at 0%, 10%, 20%, 30%, 40%, 50%, 60%,
  70%, 80%, 90%, 95%, and 99% load, using the same iperf3 and Linux
  namespace measurement method as the IPv4 NAT benchmark.

The DS-Lite launcher reads `tests/config/target_dslite_profiles.json` and shares
`tests/config/benchmark_defaults.json` with the existing IPv4 NAT launcher.
All three launchers record a non-zero child-test or inter-test recovery exit
code and continue with the remaining selected tests. After all selected tests
have been attempted, a launcher prints `DONE WITH FAILURES` and returns exit
code 1 when any failure was recorded.

Run only the DS-Lite session-generation test:

```bash
sudo python3 test_dslite_speed.py \
  --target i226_1g_dslite \
  --run-astf-sessions-cps \
  --duration 20
```

Run only the DS-Lite maximum-session test:

```bash
sudo python3 test_dslite_speed.py \
  --target i226_1g_dslite \
  --run-tcp-scratch-max-sessions
```

Run the DS-Lite TCP source-port diagnostic scan:

```bash
sudo python3 test_dslite_speed.py \
  --target i226_1g_dslite \
  --run-dslite-source-port-scan
```

This diagnostic is excluded from both no-option execution and `--run-all`.
It scans source ports `65535..1` by default and writes per-attempt results,
classified ranges, and boundary transitions. A smaller range can be selected
with `--source-start-port` and `--source-end-port`.

Run only the DS-Lite NAT stability test:

```bash
sudo python3 test_dslite_speed.py \
  --target i226_1g_dslite \
  --run-astf-natstability \
  --soak-duration-sec 3600
```

Run only the DS-Lite latency test:

```bash
sudo python3 test_dslite_speed.py \
  --target i226_1g_dslite \
  --run-l4-latency \
  --duration 30
```

# Launcher test selection

`test_basic_speed.py` uses one selection rule set:

- `--ip-version v4|v6|dual`: select IPv4, native IPv6, or both. The default is `dual`.
- `--run-all`: run the standard full set, including native IPv6 routing stability and excluding IPv4 ASTF NAT stability and DNS stability. Legacy TRex ASTF max-session tests are not included.
- No `--run-*` option: run the default set. Native IPv6 throughput/PPS and L4 latency are included; native IPv6 routing stability is excluded.
- One or more individual `--run-*` options: run only the specified tests, in launcher order.

Implemented individual selectors:

- `--run-stl-throughput`: STL throughput/PPS for the selected address family.
- `--run-tcp-scratch-max-sessions`: IPv4 TCP scratch maximum session test without TRex. This is included in the default set and `--run-all`. The scratch test prints persistent progress lines every 10 seconds by default, accepts `--progress-interval-sec` to change that interval, accepts `--session-cps` or `--session-ramp-cps` for connection creation CPS, and treats an active session as disconnected when a keepalive reply is not received within `--keepalive-timeout-sec` seconds. The default timeout is `1.0` second.
- `--run-astf-max-sessions`: legacy TRex ASTF maximum session test. This runs only when this selector is specified.
- `--run-astf-sessions-cps`: legacy TRex ASTF short-session CPS test. This runs only when this selector is specified.
- `--run-astf-natstability`: IPv4 ASTF NAT stability soak.
- `--run-ipv6-stability`: native IPv6 routing stability soak. It is also included in `--run-all`.
- `--run-l4-latency`: L4 request-response latency for the selected address family at 0%, 10%, 20%, 30%, 40%, 50%, 60%, 70%, 80%, 90%, 95%, and 99% load. The default L4 transport is UDP for both latency and iperf3 load. The 0% phase is recorded as `idle`.
- `--run-dns-latency`: DNS cache-table size, cached-response, and uncached-response benchmark.
- `--run-dns-stability`: delayed DNS cache-table recheck and fixed-QPS sustained DNS load. This is excluded from the default set and `--run-all`.
- `--run-dns-all`: run both DNS scripts.

Default example:

```bash
sudo python3 test_basic_speed.py \
  --target i226_2p5g \
  --duration 30
```

To constrain LAN-side source ports for every applicable selected test, use the
common launcher option below. The order may also be written in reverse; it is
normalized to `MIN-MAX`.

```bash
sudo python3 test_dslite_speed.py \
  --target i226_1g_dslite \
  --run-all \
  --lan-source-port-range 38564-39563
```

TCP scratch uses the complete range, STL uses `MIN` as its LAN UDP source
port, and namespace-based TCP/UDP tests (including recovery, L4, and DNS) set
the LAN namespace's automatic source-port range. TRex ASTF allocates client
source ports internally through an API that does not expose this override, and
DHCPv4 must use its protocol-defined client port 68; the launcher therefore
does not pass this option to those tests.

IPv6 only:

```bash
sudo python3 test_basic_speed.py \
  --ip-version v6 \
  --target i226_2p5g \
  --duration 30
```

All tests:

```bash
sudo python3 test_basic_speed.py \
  --ip-version dual \
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

`--run-l4-latency` normally reads the selected target profile. Fixed Linux interface names can be set in `tests/config/target_profiles.json` with `lan_iface` and `wan_iface`. Keep the PCI BDF `interfaces` entries as well; they are used when the NIC must be bound back to the kernel driver after TRex/DPDK tests.

## Configuration and shared code

Runtime defaults are stored in `tests/config/benchmark_defaults.json`. IPv4 NAT
profiles are stored in `tests/config/target_profiles.json`, DS-Lite profiles
are stored in `tests/config/target_dslite_profiles.json`, and MAP-E profiles
are stored in `tests/config/target_mape_profiles.json`. Existing
command-line options and environment-variable overrides retain precedence.

Reusable implementation code is under `tests/lib/`. The original helper module names remain available as compatibility entry points.

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

`--soak-duration-sec` is valid only when ASTF NAT stability is selected with `--run-astf-natstability`.


## ARP refresh policy

TRex-based tests wait for link-up before ARP resolution and start TRex with `--arp-refresh-period 60`. Namespace and scratch-socket tests send directed ARP plus gratuitous ARP before each phase and every 60 seconds during long-running tests. L4 latency, DNS, and TCP scratch results include `arp_refresh.csv`; DHCP scratch records per-lease ARP announcements in its event CSV.

## TRex/L4 NIC state transition

STL/ASTF tests and L4 latency tests require different NIC driver states:

- STL/ASTF: PCI devices must be bound to the TRex/DPDK driver.
- L4 latency: Linux netdevs must exist, so PCI devices must be bound to the kernel driver and then moved into `rb_lan` / `rb_wan` namespaces.

The scripts now handle this transition automatically:

- Before STL/ASTF starts, stale `rb_lan` / `rb_wan` namespaces are removed and profile PCI devices are bound to `dpdk_drivers`.
- Before L4 latency starts, profile PCI devices are bound to `kernel_drivers`.
- After L4 latency ends, namespaces are removed and profile PCI devices are bound back to `dpdk_drivers` by default.

`tests/config/target_profiles.json` must define both driver sets. Example:

```json
"i226_1g": {
    "interfaces": ["05:00.0", "04:00.0"],
    "kernel_drivers": ["igc", "igc"],
    "dpdk_drivers": ["vfio-pci", "vfio-pci"],
    "lan_iface": "enp5s0",
    "wan_iface": "enp4s0"
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


## DNS response and stability benchmarks

The default set and `--run-all` include `--run-dns-latency`. Results are written under `06_dns_forwarder_cache_latency`. The separate `--run-dns-stability` test writes under `07_dns_stability` and is excluded from the default set and `--run-all`. `--run-dns-all` selects both DNS scripts.


The DNS test starts a built-in dummy UDP DNS server on the benchmark host WAN namespace and sends DNS A queries from the LAN namespace to the router/DUT DNS forwarder. Configure the router/DUT WAN DNS server to the benchmark host WAN IP before running this test. The generated query name suffix defaults to `routerbench.example.net`.

The DNS response script executes these subtests in order:

```text
cache_table_reuse_size:
  Adds qnames at each E6 table-size step and verifies the accumulated pool at 100 QPS.
  An NG step does not stop the test immediately.
  Stops after three consecutive NG steps.

cache_rate:
  Takes 100 qnames from the final attempted cache-table step, regardless of its result.
  Reloads those same 100 qnames.
  Reuses those 100 qnames in round-robin order at cache E6 QPS steps from 10 to 48000.
  Measures latency and success rate.
  Stops when failure_rate_pct >= 50 or rtt_max_ms >= 1000.

nocache_rate:
  Uses unique qnames at the configured E6 QPS steps.
  Measures latency and success rate.
  Stops when failure_rate_pct >= 50 or rtt_max_ms >= 1000.
```

DNS-only execution:

```bash
sudo python3 test_basic_speed.py   --target i226_1g   --run-dns-latency   --duration 60
```

Short smoke test:

```bash
sudo python3 test_basic_speed.py   --target i226_1g   --run-dns-latency   --duration 3   --dns-e6-steps 10,100   --dns-skip-table
```

DNS stability uses a separate script:

```bash
sudo python3 test_basic_speed.py \
  --target i226_1g \
  --run-dns-stability \
  --dns-stability-qps 100
```

It repeats the cache-table check after the default 600-second interval, then applies a unique-qname load for 3600 seconds at the manually specified QPS. It does not perform a QPS search. The default is 100 QPS.

See `docs/benchmarks.md` for details on all test scripts.

`--repeat N` applies to every benchmark except IPv4 ASTF NAT stability,
native IPv6 routing stability, IPv4 DNS stability, and DS-Lite ASTF NAT
stability. When a benchmark requires
independent output files for each execution, runs after `--repeat 1` are stored
under `details/repeat_001`, `details/repeat_002`, and so on, with the primary
summary CSV aggregated in the benchmark output directory.

Each benchmark directory keeps its primary summary at its top level. For the
TCP scratch maximum-session test, `tcp_scratch_server_timeseries.csv` is the
primary summary and review input. It is aggregated with a `repeat` column when
`--repeat N` is greater than one. Raw CSV, diagnostic time-series data,
preflight records, metadata, generated commands and configuration, derived
summaries, and process logs are written under `details/`.

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
details/dhcp_scratch_trials.csv
details/dhcp_scratch_reconnect.csv
details/dhcp_scratch_clients.csv
details/dhcp_scratch_events.csv
details/dhcp_emu_trials.csv          # compatibility alias
details/dhcp_emu_reconnect.csv       # compatibility alias
details/dhcp_emu_clients.csv         # compatibility alias
details/dhcp_emu_events.csv          # compatibility alias
details/namespace_setup.csv
details/metadata.json
```

This test is part of the no-option default set. The result can still depend on the DUT lease table and pool size.


DNS note: the DNS benchmark returns deterministic random public-looking A records by default. This avoids common DNS rebinding protection that drops private-address DNS answers. Use `--dns-answer-mode fixed --dns-answer-base-ip 93.184.0.1` if a fixed answer address is needed.

### DNS response validation

The DNS benchmark validates the returned A record against the deterministic expected address for each qname. It reports wrong answers and RCODE categories separately. Each run uses a unique qname label, and every step requires its unique preflight qname to appear at the WAN-side test DNS server before measurement proceeds.


## Link-up settling delay

Every benchmark now waits 15 seconds after its test interfaces report link-up and before it sends benchmark traffic. The launcher and every standalone test accept `--test-start-delay-sec`; use `0` only when the DUT does not require a settling period.

## Load steps and inter-test recovery

The basic, DS-Lite, and MAP-E throughput/PPS tests use E12 load steps. Throughput runs from 1 Mbps to the selected profile's maximum speed, and the 64-byte PPS test runs from 1 kpps to the calculated profile maximum. A load series stops after packet loss is at least 50% for three consecutive points.

Before each STL measurement point, its connectivity preflight runs once by default.
Pass `--stl-recovery-probe` to enable the existing recovery loop, which checks
every two seconds and starts traffic after three consecutive successes. The
preflight still runs once when the recovery loop is disabled, and a failed
preflight prevents that measurement point from starting. For example:

```bash
sudo python3 test_basic_speed.py --target i226_1g --run-stl-throughput --stl-recovery-probe
sudo python3 test_dslite_speed.py --target i226_1g_dslite --run-stl-throughput --stl-recovery-probe
sudo python3 test_mape_speed.py --target i226_1g_mape --run-stl-throughput --stl-recovery-probe
```

Between launcher-managed tests, the existing two-second/three-success rule
remains enabled and is enforced by `tests/run_router_bench_recovery_check.py`;
results are written to `inter_test_recovery.csv`.

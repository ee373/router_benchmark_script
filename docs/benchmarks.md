## 通信事前確認の適用範囲

各項目は、試験方式に適用できる場合だけ実装する。

| 試験 | 適用する確認 | 適用しない項目と理由 |
|---|---|---|
| STL throughput/PPS | ステップごとのARP、明示MAC、NAT外部UDPポート取得、warm NAT成功判定、上り・下り32パケット確認、方向別統計 | なし |
| MAP-E STL throughput/PPS | ステップごとのARP、共有IPv4・PSIDポート取得、上り・下り32パケット確認、方向別統計 | 外側IPv6はVPPが処理するため計測器では生成・取得しない |
| ASTF max sessions / CPS | 各測定前の小規模TCP往復事前試験。接続成功と両方向L7バイトを確認 | NAT外部ポートと明示MACはASTF/TRexが動的に管理するため対象外 |
| ASTF NAT stability | 各サイクル前に同一プロトコル・同一payload directionの小規模往復事前試験 | NAT外部ポートと明示MACはASTF/TRexが動的に管理するため対象外 |
| TCP scratch max sessions | 本測定前にTCP接続でUP要求とDOWN応答を確認。DS-Lite版は同一送信元ポートから異なる2宛先への同時接続も確認 | カーネルTCP/IPがNATポートとEthernetヘッダーを管理するため外部ポート取得・明示MACは対象外 |
| DS-Lite TCP source-port scan | 自動送信元ポートによるTCP往復事前確認後、指定範囲の各送信元ポートでTCP接続とバイナリ要求・応答を確認 | 診断専用のPythonソケット試験であり、TRex、iperf3、外部ポート取得は使用しない |
| L4 latency | 各フェーズ前の既存TCP/UDP request-response preflight | カーネルソケット試験のため外部ポート取得・明示MACは対象外 |
| DNS | 各負荷ステップ前に3クエリの要求・応答確認。事前クエリ分はWAN問い合わせ数から除外 | DNS forwarder試験であり、固定NAT外部UDPポートを生成しない |
| DHCP scratch | 各試行自体がDISCOVER/OFFER/REQUEST/ACKの要求・応答確認。クライアントMACはDHCPフレームに明示済み | NAT、warm NAT、双方向負荷統計は対象外 |

# Router benchmark tests

This directory contains the individual benchmark implementations used by
`test_basic_speed.py`, `test_dslite_speed.py`, and `test_mape_speed.py`.

The launcher is the normal entry point:

```bash
sudo python3 test_basic_speed.py --target i226_1g --duration 60
```

Individual tests can also be executed directly when debugging a specific subsystem.

## File map

| File | Role |
|---|---|
| `tests/config/benchmark_defaults.json` | Launcher, TRex, and measurement defaults. |
| `tests/config/target_profiles.json` | Hardware, PCI, driver, port, and IPv4 profile definitions. |
| `tests/config/target_dslite_profiles.json` | DS-Lite hardware, link, AFTR NAT-pool, MTU, and tunnel profile definitions. |
| `tests/config/target_mape_profiles.json` | MAP-E hardware, IPv4 measurement link, rule, shared IPv4, PSID, MTU, BR, and server definitions. |
| `tests/lib/` | Shared benchmark implementation modules. |
| `tests/config/` | JSON defaults and target profiles. |
| `unit_tests/` | Hardware-independent configuration and launcher tests. |
| `tests/run_router_bench_v4_stl_throughput_pps.py` | TRex STL IPv4 NAT throughput and PPS benchmark. |
| `tests/run_router_bench_v6_stl_throughput_pps.py` | TRex STL native IPv6 routing throughput and PPS benchmark. |
| `tests/run_router_bench_dslite_stl_throughput_pps.py` | TRex STL DS-Lite IPv4 throughput and PPS benchmark. |
| `tests/run_router_bench_mape_stl_throughput_pps.py` | TRex STL MAP-E throughput and PPS benchmark with IPv4 on both tester ports and VPP-owned encapsulation. |
| `tests/run_router_bench_dslite_tcp_scratch_max_sessions.py` | DS-Lite TCP maximum concurrent session benchmark using multiple WAN server IPv4 addresses. |
| `tests/run_router_bench_dslite_tcp_source_port_scan.py` | Explicit-only DS-Lite TCP source-port reachability scan using built-in Python sockets. |
| `tests/run_router_bench_dslite_astf_sessions_cps.py` | TRex ASTF DS-Lite short-session TCP CPS benchmark. |
| `tests/run_router_bench_dslite_astf_nat_stability.py` | TRex ASTF DS-Lite TCP/UDP NAT stability soak benchmark. |
| `tests/run_router_bench_dslite_iperf3_latency.py` | DS-Lite TCP/UDP request-response latency benchmark with iperf3 load. |
| `tests/run_router_bench_v4_tcp_scratch_max_sessions.py` | TCP scratch maximum concurrent session benchmark without TRex. Included in the default set and `--run-all`. |
| `tests/run_router_bench_v4_astf_max_sessions.py` | Legacy TRex ASTF maximum concurrent session benchmark. Launcher execution requires `--run-astf-max-sessions`. |
| `tests/run_router_bench_v4_astf_sessions_cps.py` | TRex ASTF short-session CPS benchmark. Included in the default set and `--run-all`; can also be selected with `--run-astf-sessions-cps`. |
| `tests/run_router_bench_v4_astf_nat_stability.py` | TRex ASTF TCP/UDP NAT stability soak benchmark. |
| `tests/run_router_bench_v6_astf_routing_stability.py` | TRex ASTF TCP/UDP native IPv6 routing stability soak benchmark. Included only by `--run-all` or its explicit selector. |
| `tests/run_router_bench_v4_iperf3_latency.py` | Linux namespace based TCP/UDP L4 request-response latency benchmark with optional iperf3 load. |
| `tests/run_router_bench_v6_iperf3_latency.py` | Native IPv6 Linux namespace TCP/UDP L4 request-response latency benchmark with optional iperf3 load. |
| `tests/run_router_bench_v4_dns_latency.py` | DNS cache-table size, cached-response, and uncached-response benchmark. |
| `tests/run_router_bench_v4_dns_stability.py` | DNS delayed table recheck and fixed-QPS sustained-load benchmark. |
| `tests/run_router_bench_v4_dhcp_scratch.py` | DHCPv4 basic DORA latency, burst, and same-MAC reconnect benchmark using a scratch raw-socket client. |

Only `run_*.py` files and the package marker are kept directly under
`tests/`. Shared code is under `tests/lib/`, configuration is under
`tests/config/`, and hardware-independent tests are under `unit_tests/`.

All benchmark entry points accept `--repeat` except
`run_router_bench_v4_astf_nat_stability.py`,
`run_router_bench_v6_astf_routing_stability.py`,
`run_router_bench_v4_dns_stability.py`, and
`run_router_bench_dslite_astf_nat_stability.py`. For benchmarks that execute
the complete sequence separately, `--repeat N` writes each execution under
`details/repeat_NNN` and aggregates the primary summary CSV in the parent
output directory.

## Result directory layout

Each benchmark result directory keeps its primary summary CSV or JSON at the
top level. Raw counters, diagnostic time-series data, preflight results,
metadata, generated commands and configuration, packet/query records, and
process logs are written under `details/`. For the TCP scratch maximum-session
test, `tcp_scratch_server_timeseries.csv` is the primary summary because it is
the review input.

```text
<benchmark-result>/
  summary_all.csv
  tcp_scratch_server_timeseries.csv  # TCP scratch maximum-session summary
  details/
    metadata.json
    ... raw data and diagnostic logs ...
```

For `--repeat N`, the aggregated summary remains at the top level. The TCP
scratch maximum-session test aggregates `tcp_scratch_server_timeseries.csv`
with a `repeat` column at the top level. Per-run results and
`repeat_metadata.json` are written under `details/`.

## Launcher order

The launcher executes tests in this order:

```text
01_stl_throughput_pps
02_tcp_scratch_max_sessions
02_astf_max_sessions_legacy       # explicit selector only
03_astf_sessions_cps
04_astf_nat_stability             # explicit selector only
05_iperf3_l4rr_latency
06_dns_forwarder_cache_latency
07_dns_stability                    # explicit selector only
08_dhcp_scratch_lease_acquisition
09_ipv6_stl_throughput_pps
10_ipv6_iperf3_l4rr_latency
11_ipv6_routing_stability           # --run-all or explicit selector only
```

The separate DS-Lite launcher currently executes:

```text
01_dslite_stl_throughput_pps
02_dslite_tcp_scratch_max_sessions
03_dslite_astf_sessions_cps
04_dslite_astf_nat_stability     # --run-all or explicit selector only
05_dslite_iperf3_l4rr_latency
special_dslite_tcp_source_port_scan  # explicit selector only; excluded from --run-all
```

The basic, DS-Lite, and MAP-E launchers continue after a child-test or
inter-test recovery process returns a non-zero exit code. Each launcher records
every failure, attempts all remaining selected tests, then prints
`DONE WITH FAILURES` and returns exit code 1. A completely successful selection
prints `DONE` and returns exit code 0.

The separate MAP-E launcher currently executes:

```text
01_mape_stl_throughput_pps
```

### Common LAN source-port range

All three launchers accept `--lan-source-port-range MIN-MAX`. For example:

```bash
sudo python3 test_dslite_speed.py \
  --target i226_1g_dslite \
  --run-tcp-scratch-max-sessions \
  --lan-source-port-range 38564-39563
```

| Test path | Effect |
|---|---|
| TCP scratch maximum sessions | Uses every source port from `MIN` through `MAX`. For DS-Lite, the default 68 WAN destinations multiply this range into the planned session capacity. The multi-destination preflight uses `MIN`. |
| STL throughput/PPS | Uses `MIN` as the LAN-side UDP source port for warm-up, probes, and measured traffic. |
| Namespace TCP/UDP tests | Sets `net.ipv4.ip_local_port_range` inside the LAN namespace before recovery, L4, or DNS traffic starts. |
| TRex ASTF | Not applied. The public ASTF profile API used here allocates client source ports internally and does not provide this override. |
| DHCPv4 | Not applied. DHCP clients use UDP source port 68. |

The parser accepts ascending or descending input and records the normalized
`MIN-MAX` value in launcher metadata. Ports must be in `1024..65535`.

`test_basic_speed.py` defaults to `--ip-version dual`. No-option launcher execution adds native IPv6 STL throughput/PPS and native IPv6 L4 latency to the existing IPv4 default set. Native IPv6 routing stability is excluded from no-option execution and included by `--run-all`. IPv4 ASTF NAT stability and DNS stability remain excluded from `--run-all`.

## Shared target profiles

`config/target_profiles.json` is the single source of target information. The main profile fields are:

| Field | Meaning |
|---|---|
| `interfaces` | PCI BDF list used by TRex/DPDK. |
| `kernel_drivers` | Kernel drivers used by namespace based tests. |
| `dpdk_drivers` | DPDK drivers used by TRex STL/ASTF tests. `auto` enables driver auto-selection. |
| `lan_iface`, `wan_iface` | Linux netdev names used by L4/DNS namespace tests. |
| `lan_ipv6`, `lan_ipv6_gw` | Native IPv6 LAN tester and router addresses. Defaults: `fd00:1::2`, `fd00:1::1`. |
| `wan_ipv6`, `wan_ipv6_gw`, `router_wan_ipv6` | Native IPv6 WAN tester and router addresses. Defaults: tester `fd00:100::1`, router `fd00:100::2`. |
| `lan_port`, `wan_port` | TRex port numbers. |
| `lan_ip`, `lan_gw`, `wan_ip`, `wan_gw`, `router_wan_ip` | Addressing used by namespace tests and metadata. |
| `line_rate_gbps` | Line rate used by load percentage calculations. |
| `trex_cores` | Default TRex core count used by STL and as the ASTF fallback. |
| `astf_cores` | Optional ASTF-specific DP core count. The X540 profile uses 1 because its ASTF client/server pools contain one address per side; STL retains 2 cores for 10GbE. |

The `--cores` option is an explicit override and sets both `trex_cores` and `astf_cores`. For normal `x540_10g` execution, omit `--cores`: STL starts with 2 DP cores and ASTF starts with 1 DP core.

Direct L2 loop baseline profiles are also defined for L4 latency baseline measurement:

```text
i226_1g_loop
i226_2p5g_loop
x540_10g_loop
```

Those profiles declare `supported_tests = ["l4_latency"]` and are rejected by the launcher for TRex router/NAT tests.

## MAP-E benchmarks

Launcher:

```bash
sudo python3 test_mape_speed.py \
  --target i226_1g_mape \
  --duration 20 \
  --repeat 1
```

Direct execution:

```bash
sudo python3 tests/run_router_bench_mape_stl_throughput_pps.py \
  --target i226_1g_mape \
  --duration 20 \
  --repeat 1
```

TRex sends and receives native IPv4 on both ports. The default addresses are:

| Side | TRex IPv4 | Gateway/ARP target |
|---|---|---|
| LAN | `192.168.0.2` | `192.168.0.1` |
| WAN | `198.51.100.254` | `198.51.100.1` (VPP) |

VPP performs IPv4-in-IPv6 encapsulation and decapsulation. Consequently, the
TRex script neither creates outer IPv6 packets nor configures a CE MAP IPv6
source. `ce_map_ipv6` remains `null`; `ce_wan_ia_na` is retained only as
control-plane profile information.

Each step sends IPv4 from `192.168.0.2` to `203.0.113.2`, captures the
decapsulated IPv4 packet on the WAN port, and requires:

- translated source IPv4 `198.18.0.12`;
- destination IPv4 `203.0.113.2`;
- translated source port in the configured PSID set;
- unchanged mapping during the warm and uplink probes.

The downlink packet uses source `203.0.113.2`, destination `198.18.0.12`, and
the translated destination port captured during the uplink probe. VPP then
performs the downlink MAP-E encapsulation. The default PSID parameters produce
240 ports, beginning with `4144-4159` and ending with `61488-61503`.

The large inner frame is 1478 bytes including Ethernet FCS, corresponding to a
1460-byte IPv4 packet. `outer_line_estimated_bps` includes the 40-byte MAP-E
IPv6 header even though TRex itself handles only IPv4 packets.

## DS-Lite benchmarks

Launcher:

```bash
sudo python3 test_dslite_speed.py \
  --target i226_1g_dslite \
  --duration 20 \
  --repeat 1
```

Direct execution:

```bash
sudo python3 tests/run_router_bench_dslite_stl_throughput_pps.py \
  --target i226_1g_dslite \
  --duration 20 \
  --repeat 1
```

The WAN TRex port is configured with `wan_link_ip` and ARPs only
`wan_arp_target`. The AFTR NAT-pool address is never used as an ARP target.
Before every measurement step, the script sends an uplink UDP probe, captures
the translated source IPv4 address and UDP port, verifies that the IPv4 address
belongs to `nat_pool_cidr`, and uses the discovered pair for downlink traffic.

Frame sizes include Ethernet FCS. The default large frame is 1478 bytes:
14-byte Ethernet header, 1460-byte inner IPv4 packet, and 4-byte FCS. The
summary records the inner IPv4 rate and the estimated outer line rate after the
configured IPv6 tunnel overhead:

```text
inner_ipv4_bps
outer_line_estimated_bps
theoretical_outer_line_bps
outer_line_reach_rate
```

Current target profiles:

```text
i226_1g_dslite
i226_2p5g_dslite
x540_10g_dslite
```

The profiles enable both high-CPS and maximum-session tests. The configured
AFTR B4 session limit is 1000.

### DS-Lite maximum concurrent sessions

Launcher:

```bash
sudo python3 test_dslite_speed.py \
  --target i226_1g_dslite \
  --run-tcp-scratch-max-sessions
```

Direct execution:

```bash
sudo python3 tests/run_router_bench_dslite_tcp_scratch_max_sessions.py \
  --target i226_1g_dslite
```

VPP DS-Lite identifies an in2out mapping by the B4 IPv6 address, inner IPv4
source address, source port, and protocol. The remote destination is not part
of that key. Therefore, connections from one internal source port to different
server addresses reuse the same AFTR external address and port.

The default test keeps the existing TCP scratch behavior: 100 CPS creation,
one framed UP/DOWN keepalive per second, a one-second keepalive timeout, and a
600-second hold after the ramp. It uses 1000 source ports (`65535..64536`) and
68 WAN server addresses (`198.51.100.254..198.51.100.187`) to create 68,000
sessions. Before measurement, it confirms simultaneous TCP exchanges from one
source port to the first and last server addresses.

As with the IPv4 TCP scratch maximum-session test,
`tcp_scratch_server_timeseries.csv` is written at the result-directory top
level for review.

Useful options:

```text
--max-sessions
--server-ip-count
--source-start-port / --source-end-port
--lan-source-port-range
--session-cps / --session-ramp-cps
--post-create-hold-sec
--keepalive-interval-sec
--keepalive-timeout-sec
```

### DS-Lite TCP source-port scan

This diagnostic test is selected only with its dedicated launcher option. It
is not included in no-option execution or `--run-all`.

```bash
sudo python3 test_dslite_speed.py \
  --target i226_1g_dslite \
  --run-dslite-source-port-scan
```

The implementation uses a Python TCP server in `rb_wan` and a Python client in
`rb_lan`. Each client socket binds the port under test, completes the TCP
handshake, sends a fixed binary request, verifies the reply, and closes with
RST to release TCP and translation state. The default scan covers
`65535..1` at 100 connection attempts per second with 32 concurrent attempts.
After each 500-port batch, the scanner retests the source port that passed the
initial preflight. If that baseline port does not recover within 300 seconds,
the scan stops and marks the result incomplete. This prevents an AFTR session
limit or delayed translation cleanup from invalidating all later port results.

Before scanning, the client uses automatically allocated source ports until
the configured preflight requirement is met. This distinguishes a source-port
result from a server, route, or DS-Lite connectivity failure.

Useful options:

```text
--source-start-port / --source-end-port
--scan-step
--scan-cps
--concurrency
--scan-batch-size
--connect-timeout-sec
--io-timeout-sec
--repeat
--preflight-attempts
--preflight-success-consecutive
--recovery-probe-interval-sec
--recovery-success-consecutive
--recovery-timeout-sec
```

Primary outputs:

```text
source_port_scan_results.csv
source_port_scan_ranges.csv
source_port_scan_summary.json
```

### DS-Lite short-session TCP CPS

Launcher:

```bash
sudo python3 test_dslite_speed.py \
  --target i226_1g_dslite \
  --run-astf-sessions-cps \
  --duration 20
```

Direct execution:

```bash
sudo python3 tests/run_router_bench_dslite_astf_sessions_cps.py \
  --target i226_1g_dslite \
  --duration 20 \
  --repeat 1
```

The test uses the existing IPv4 NAT ASTF short-session implementation. TRex
generates a one-byte TCP request and response for every connection, then resets
the client side. Each CPS step first performs a small TCP round-trip preflight
and verifies successful establishment plus L7 bytes in both directions.

The search uses the existing `cps_steps`, `stable_success_threshold`,
`stable_loss_threshold`, and stop conditions. The result directory contains
`summary_all.csv`; `astf_raw.csv`, `metadata.json`, `trex_cfg.yaml`,
`trex_interface_prepare.csv`, and `trex_server_astf.log` are under `details/`.

### DS-Lite TCP/UDP NAT stability

Launcher:

```bash
sudo python3 test_dslite_speed.py \
  --target i226_1g_dslite \
  --run-astf-natstability \
  --soak-duration-sec 3600
```

Direct execution:

```bash
sudo python3 tests/run_router_bench_dslite_astf_nat_stability.py \
  --target i226_1g_dslite \
  --soak-duration-sec 3600
```

The DS-Lite script uses the existing IPv4 NAT stability implementation. It
runs TCP and UDP by default. Each cycle creates 200 flows at 50 CPS, transfers
1 MB per flow from server to client, verifies a protocol-matched round trip
before measurement, and applies the same completion and stability thresholds.
The DS-Lite profile supplies the WAN link and gateway aliases, AFTR NAT-pool
metadata, and a default TCP MSS of 1420 bytes.

The test is excluded from the no-selector default plan. Use
`--run-astf-natstability` for this test only, or `--run-all` for the complete
DS-Lite plan.

### DS-Lite TCP/UDP request-response latency

Launcher:

```bash
sudo python3 test_dslite_speed.py \
  --target i226_1g_dslite \
  --run-l4-latency \
  --duration 30
```

Direct execution:

```bash
sudo python3 tests/run_router_bench_dslite_iperf3_latency.py \
  --target i226_1g_dslite \
  --duration 30
```

The DS-Lite script uses the existing IPv4 NAT latency implementation. The LAN
client and WAN IPv4 server execute in separate Linux network namespaces, so
traffic traverses the DUT B4 and VPP AFTR path. It measures TCP or UDP
request-response RTT while iperf3 generates load at 0%, 10%, 20%, 30%, 40%,
50%, 60%, 70%, 80%, 90%, 95%, and 99% of the target line rate. UDP is the
default transport for both latency and load.

Each phase performs the existing request-response preflight. `summary_all.csv`
is the primary result. Per-sample CSV files, iperf3 JSON, `metadata.json`,
`namespace_setup.csv`, and `arp_refresh.csv` are under `details/`. The test is
included in both the no-selector default plan and `--run-all`.

## 01: STL throughput/PPS

Script:

```text
run_router_bench_v4_stl_throughput_pps.py
```

Purpose:

- Generates IPv4 NAT traffic with TRex STL.
- Measures throughput and PPS for 64-byte and large-frame traffic.
- Uses E12 load steps. Throughput starts at 1 Mbps and reaches the configured line rate; 64-byte PPS starts at 1 kpps and reaches the calculated profile maximum.
- Stops the remaining load steps after three consecutive points with at least 50% loss.
- Writes NDR/PDR summary rows.
- Before every rate step, refreshes ARP on both ports and writes explicit source/destination MAC addresses into every generated frame.
- Before every rate step, sends warm-up NAT traffic, captures the WAN-side translated UDP source port, and uses that port for downlink traffic.
- Before every rate step, verifies 32 uplink packets and 32 downlink packets. By default, one preflight is run; `--stl-recovery-probe` enables retries every two seconds until three consecutive checks succeed.
- Bidirectional rows include combined counters and independent `up_*` / `down_*` counters and loss rates.

Main outputs:

```text
summary_all.csv
details/stl_raw.csv
details/stl_preflight.csv
details/stl_recovery.csv
details/metadata.json
details/trex_cfg.yaml
details/trex_interface_prepare.csv
details/trex_server_stl.log
```

Useful direct execution:

```bash
sudo python3 tests/run_router_bench_v4_stl_throughput_pps.py \
  --target i226_1g \
  --duration 60 \
  --repeat 1
```

Performance notes:

```text
The WAN DNS server and LAN DNS client run as separate processes in separate network namespaces.
The WAN DNS server runs packet response and WAN-query counter publishing separately.
The counter used by summary_all.csv includes only generated benchmark qnames and is read from dns_server_queries.csv.stats.json. Unrelated router-originated DNS traffic is counted separately and does not affect cache overflow detection.
Successful WAN queries are never written to dns_server_queries.csv. That file contains only server-side failures with the minimal columns unix_ts, qname, and error. The default failure-row limit is 1000; additional failures remain counted in the stats sidecar as failure_log_dropped.
By default, LAN-side measurement keeps only raw send/receive timestamps and minimal DNS status fields during each measurement step. Success rate, failure rate, timeout classification, latency percentiles, and per-query CSV rows are calculated after the timed interval ends. This avoids both per-packet disk I/O and per-packet aggregation work during the timed interval.
Use --dns-sample-limit 0 to disable LAN sample rows or --dns-server-query-log-limit 0 to disable server-side failure rows.

DNS stability: `run_router_bench_v4_dns_stability.py` waits `--dns-table-reuse-interval-sec` seconds before the delayed `cache_table_size` pass, then applies a sustained unique-qname load at `--dns-stability-qps`. The defaults are 600 seconds, 100 QPS, and 3600 seconds. The stability script is excluded from no-option execution and `--run-all`.
```

Note: the default DNS answer mode returns deterministic random public-looking IPv4 addresses. Some routers apply DNS rebinding protection and suppress answers that contain private or special-use addresses such as `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, or `100.64.0.0/10`. If WAN-side query logs increase but LAN-side success remains 0%, check this setting first.

Useful options:

```text
--duration
--repeat
--rate-search / --no-rate-search
--rate-mbps-steps
--refine-step
--single-load-percent
--lossless-threshold
--stop-on-first-loss
--no-auto-dpdk-bind
--dpdk-driver
--interfaces
--lan-ip / --lan-gw / --wan-ip / --wan-gw / --router-wan-ip
--line-rate-gbps
--cores
```


## 02: TCP scratch maximum sessions

Script: `run_router_bench_v4_tcp_scratch_max_sessions.py`

Purpose:

- Measures NAT/TCP session capacity without TRex.
- Starts a Python TCP server in the WAN namespace.
- Creates LAN-side TCP sessions with source ports from `65535` down to `1024`.
- Uses `100 CPS` by default. The rate is controlled by `--session-cps`; `--session-ramp-cps` is also accepted as an alias for compatibility with the legacy ASTF max-session option name.
- Sends one framed JSON payload per active session every second. The payload contains `source_port`, `elapsed_sec`, and `direction=UP`; the WAN server replies with `direction=DOWN`. If the DOWN reply for a keepalive is not received within `--keepalive-timeout-sec` seconds, the session is marked disconnected. The default timeout is `1.0` second.
- After all source ports have been attempted, keeps the sessions active for `600` seconds by default.
- Prints one persistent progress line every `10` seconds by default. This interval is controlled by `--progress-interval-sec`.

Result files:

- `tcp_scratch_server_timeseries.csv` at the result-directory top level
  - This file is the primary summary and review input.
  - Includes `target_session_attempts_total` and `target_session_attempts_delta`, copied from the LAN-side session creation status so WAN accept counts can be compared with attempted connection counts.
- `details/tcp_scratch_derived_summary.csv`
- `details/tcp_scratch_derived_summary.json`
- `details/tcp_scratch_max_sessions_timeseries.csv`
- `details/tcp_scratch_max_sessions_sessions.csv`
- `details/tcp_scratch_max_sessions_summary.json`
- `details/tcp_scratch_server_summary.json`

Important counters:

- `session_create_success_total`
- `session_create_fail_total`
- `session_mid_disconnect_total`
- `keepalive_timeout_total`
- `current_active_sessions`
- `*_delta` fields
- `payload_message_loss_rate`
- `target_session_attempts_total` / `target_session_attempts_delta` in `tcp_scratch_server_timeseries.csv`

Launcher:

```bash
sudo python3 test_basic_speed.py --target i226_1g --run-tcp-scratch-max-sessions --session-ramp-cps 33
```

This test is part of the no-option default set and `--run-all`.

Useful options:

```text
--session-cps
--session-ramp-cps
--source-start-port / --source-end-port
--post-create-hold-sec
--keepalive-interval-sec
--keepalive-timeout-sec
--progress-interval-sec
```

## 02: legacy ASTF maximum sessions

Script:

```text
run_router_bench_v4_astf_max_sessions.py
```

Purpose:

- Uses TRex ASTF to test NAT session scale.
- Runs the maximum concurrent session test.
- Records 1-second time-series data for the maximum-session phase.

Main outputs:

```text
summary_all.csv
details/astf_raw.csv
details/astf_max_sessions_timeseries.csv
details/metadata.json
details/trex_server_astf.log
```

Useful direct execution:

```bash
sudo python3 tests/run_router_bench_v4_astf_max_sessions.py \
  --target i226_1g \
  --max-sessions 65535 \
  --session-ramp-cps 100 \
  --repeat 1
```

Useful options:

```text
--max-sessions
--session-ramp-cps
--session-hold-sec
--astf-tcp-port
--show-progress / --no-progress
--progress-interval
--progress-percent-step
--no-auto-dpdk-bind
--dpdk-driver
--interfaces
--lan-ip / --lan-gw / --wan-ip / --wan-gw / --router-wan-ip
--line-rate-gbps
--cores
```

## 03: ASTF short-session CPS

Script:

```text
run_router_bench_v4_astf_sessions_cps.py
```

Purpose:

- Uses TRex ASTF to test short-session NAT creation rate.
- Runs a short-session CPS search.
- This benchmark is part of the no-option default set and `--run-all`.

Main outputs:

```text
summary_all.csv
details/astf_raw.csv
details/metadata.json
details/trex_server_astf.log
```

Useful direct execution:

```bash
sudo python3 tests/run_router_bench_v4_astf_sessions_cps.py \
  --target i226_1g \
  --duration 60 \
  --repeat 1
```

Useful options:

```text
--max-cps
--cps-steps
--representative-cps
--loss-stop-threshold
--session-success-stop-threshold
--stable-success-threshold
--stable-loss-threshold
--cps-teardown-timeout
--astf-tcp-port
--no-auto-dpdk-bind
--dpdk-driver
--interfaces
--lan-ip / --lan-gw / --wan-ip / --wan-gw / --router-wan-ip
--line-rate-gbps
--cores
```

## 04: ASTF NAT stability soak

Script:

```text
run_router_bench_v4_astf_nat_stability.py
```

Purpose:

- Uses TRex ASTF to run repeated NAT stability cycles.
- Default pattern runs TCP and UDP cycles.
- Each cycle creates 200 flows at 50 CPS by default.
- Each flow transfers 1 MB by default, then the flow is explicitly completed or closed.
- The next cycle begins after the configured inter-cycle sleep.

Main outputs:

```text
summary_all.csv
details/summary_tcp.csv
details/summary_udp.csv
details/astf_nat_stability_tcp_cycles.csv
details/astf_nat_stability_tcp_raw.csv
details/astf_nat_stability_udp_cycles.csv
details/astf_nat_stability_udp_raw.csv
details/metadata.json
details/trex_server_astf.log
```

Useful direct execution:

```bash
sudo python3 tests/run_router_bench_v4_astf_nat_stability.py \
  --target i226_1g \
  --soak-duration-sec 3600
```


Note: the default DNS answer mode returns deterministic random public-looking IPv4 addresses. Some routers apply DNS rebinding protection and suppress answers that contain private or special-use addresses such as `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, or `100.64.0.0/10`. If WAN-side query logs increase but LAN-side success remains 0%, check this setting first.

Useful options:

```text
--soak-duration-sec
--protocols
--cycle-cps
--cycle-sessions
--mb-per-session
--payload-bytes-per-session
--payload-direction
--inter-cycle-sleep-sec
--cycle-hard-timeout-sec
--connect-timeout-sec
--teardown-timeout-sec
--generation-grace-sec
--completion-quiet-sec
--active-flow-complete-threshold
--sample-interval-sec
--stable-success-threshold
--stable-loss-threshold
--payload-completion-threshold
--payload-chunk-bytes
--udp-msg-payload-bytes
--udp-mtu
--tcp-mss
--tcp-window-kb
--astf-tcp-port
--astf-udp-port
--client-port-min
--client-port-max
--no-auto-dpdk-bind
--dpdk-driver
--interfaces
--lan-ip / --lan-gw / --wan-ip / --wan-gw / --router-wan-ip
--line-rate-gbps
--cores
```

## 05: iperf3 L4 request-response latency

Script:

```text
run_router_bench_v4_iperf3_latency.py
```

Purpose:

- Measures application-layer TCP or UDP request-response RTT.
- Can measure idle RTT and RTT under iperf3 load.
- Default load phases are 0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, and 99 percent of the target line rate.
- Uses Linux network namespaces so traffic must traverse the DUT.
- Rebinds NICs from DPDK back to kernel drivers when required.

Topology:

```text
LAN namespace -> DUT/router -> WAN namespace
```

Main outputs:

```text
summary_all.csv
details/latency_idle_samples.csv
details/latency_load_10pct_samples.csv ... details/latency_load_99pct_samples.csv
details/l4rr_client_idle.json
details/l4rr_client_load_10pct.json ... details/l4rr_client_load_99pct.json
details/iperf3_client_load_10pct.json ... details/iperf3_client_load_99pct.json
details/metadata.json
details/namespace_setup.csv
```

Useful direct execution:

```bash
sudo python3 tests/run_router_bench_v4_iperf3_latency.py \
  --target i226_1g \
  --duration 30 \
  --l4-transport udp \
  --load-direction download
```


Note: the default DNS answer mode returns deterministic random public-looking IPv4 addresses. Some routers apply DNS rebinding protection and suppress answers that contain private or special-use addresses such as `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, or `100.64.0.0/10`. If WAN-side query logs increase but LAN-side success remains 0%, check this setting first.

Useful options:

```text
--duration
--latency-duration-sec
--latency-interval-sec
--latency-payload-size-bytes
--latency-timeout-sec
--latency-protocol
--load-protocol
--l4-transport
--load-percentages
--load-direction
--no-l4rr-preflight
--setup-namespaces / --no-setup-namespaces
--replace-namespaces / --no-replace-namespaces
--keep-namespaces
--auto-kernel-bind / --no-auto-kernel-bind
--restore-dpdk-after / --no-restore-dpdk-after
--lan-iface / --wan-iface
--lan-ns / --wan-ns
--post-namespace-settle-sec
--interface-ready-timeout-sec
--dpdk-driver
```

## 06: DNS cache table/cached/uncached response benchmark

Script:

```text
run_router_bench_v4_dns_latency.py
```

Purpose:

- Starts a built-in dummy UDP DNS server in the WAN namespace.
- Sends DNS A queries from the LAN namespace to the DUT DNS forwarder.
- Measures approximate DNS cache table size by checking whether re-queries reach the WAN DNS server.
- Measures cache-hit DNS response performance at cache E6 QPS steps from 10 to 48000.
- Measures non-cache DNS forwarding performance at E6 QPS steps.

Topology:

```text
LAN namespace DNS client -> DUT DNS forwarder/cache -> WAN namespace dummy DNS server
```

Router/DUT preparation:

- Configure the DUT WAN DNS server to the benchmark host WAN IP.
- For the built-in `i226_1g` and `i226_2p5g` profiles, this is normally `192.168.100.1`.

Default generated names use the suffix:

```text
routerbench.example.net
```

Example generated name (the middle label is unique for each benchmark run):

```text
r000000001.run20260717t120000-a1b2c3d4.routerbench.example.net
```

Before every DNS measurement step, the script sends three queries for a step-specific preflight name. Measurement starts only when all three LAN responses contain the exact expected A record and that exact preflight qname is observed by the WAN-side test DNS server. A valid-looking response from another upstream DNS server, a DNS interception feature, or an old cache entry is recorded as `ERROR` in `summary_all.csv`. The dependent steps in that DNS item are skipped, and the script continues with the next independent DNS item. Namespace setup failure, interface failure, and WAN DNS server startup failure remain fatal because measurement cannot begin in those cases.

Default DNS subtests, in execution order:

```text
cache_table_reuse_size:
  Runs before the DNS rate tests.
  E6 table-size steps use an independent qname range.
  Each larger step adds only the missing qnames, then re-queries the whole accumulated pool at 100 QPS.
  Only generated benchmark qnames are included in wan_queries_delta; unrelated router background DNS traffic is excluded.
  A step is NG when failure_rate_pct >= 50 or wan_queries_delta / cache_table_size >= 50%.
  An NG does not stop the test immediately. The subtest stops after three consecutive NG steps.

cache_rate:
  Selects the first 100 qnames from the final attempted cache-table step, regardless of that step's result.
  The same 100 qnames are reloaded into the DUT DNS cache before measurement.
  The same 100 qnames are then queried in round-robin order at cache E6 QPS steps from 10 to 48000.
  Each QPS step uses --duration seconds.
  The subtest stops when failure_rate_pct >= 50 or rtt_max_ms >= 1000.

nocache_rate:
  Unique qnames are queried for each request at the configured E6 QPS steps.
  Each QPS step uses --duration seconds.
  The subtest stops when failure_rate_pct >= 50 or rtt_max_ms >= 1000.
```

Main outputs:

```text
summary_all.csv
details/dns_server_queries.csv                 server-side failures only; minimal columns
details/dns_server_queries.csv.stats.json      all query counters and failure counters
details/dns_<phase>_<stage>_<step>_samples.csv
details/dns_<phase>_<stage>_<step>.json
details/metadata.json
details/namespace_setup.csv
details/dns_server.stdout.log
details/dns_server.stderr.log
```

Important summary columns:

```text
phase
stage
step_value
target_qps
actual_qps
success_rate_pct
failure_rate_pct
fill_rate_pct
responses_received_total
wrong_answer_count
rcode_noerror_count
rcode_servfail_count
rcode_nxdomain_count
rcode_refused_count
preflight_wan_qname_seen
preflight_expected_ip
preflight_observed_ips
rtt_p50_ms
rtt_p95_ms
rtt_p99_ms
rtt_max_ms
wan_queries_delta
cache_table_size
cache_overflow_detected
cache_overflow_rate_pct
stop_reason
```

Useful direct execution:

```bash
sudo python3 tests/run_router_bench_v4_dns_latency.py \
  --target i226_1g \
  --duration 60
```

Launcher execution for DNS only:

```bash
sudo python3 test_basic_speed.py \
  --target i226_1g \
  --run-dns-latency \
  --duration 60
```

Short smoke test:

```bash
sudo python3 test_basic_speed.py \
  --target i226_1g \
  --run-dns-latency \
  --duration 3 \
  --dns-e6-steps 10,100 \
  --dns-skip-table
```


Note: the default DNS answer mode returns deterministic random public-looking IPv4 addresses. Some routers apply DNS rebinding protection and suppress answers that contain private or special-use addresses such as `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, or `100.64.0.0/10`. If WAN-side query logs increase but LAN-side success remains 0%, check this setting first.

Useful options:

```text
--duration
--dns-port
--dns-domain-suffix
--dns-run-label       default: automatically generated unique DNS label
--dns-answer-mode     default: random
--dns-answer-base-ip  default: 93.184.0.1, used only with --dns-answer-mode fixed
--dns-ttl-sec
--dns-timeout-sec
--dns-sample-limit        default: -1, log all per-step LAN samples after post aggregation
--dns-server-query-log-limit  default: 1000, maximum server-side failure rows; 0 disables rows and -1 keeps all failures
--dns-socket-buffer-bytes
--dns-server-log-batch-size  retained for compatibility
--dns-server-log-flush-sec   stats sidecar update interval; failure CSV is written at server shutdown
--dns-server-start-delay-sec
--dns-client-server-ip
--dns-e6-steps
--dns-cache-e6-steps
--dns-record-count
`--dns-record-count 0` uses automatic sizing. Automatic sizing now reserves separate qname ranges for nocache, cache preload, and table-size phases. If a fixed record count is specified, it must cover the sum of all selected nocache E6 steps for the selected `--duration`, the cache preload count, and the largest table-size step.

--dns-cache-preload-count
--dns-table-qps
--dns-table-overflow-rate-pct
--dns-stop-failure-rate-pct
--dns-stop-response-ms
--dns-randomize
--dns-skip-nocache
--dns-skip-cache
--dns-skip-table
--setup-namespaces / --no-setup-namespaces
--replace-namespaces / --no-replace-namespaces
--keep-namespaces
--auto-kernel-bind / --no-auto-kernel-bind
--restore-dpdk-after / --no-restore-dpdk-after
--lan-iface / --wan-iface
--lan-ns / --wan-ns
--lan-prefix / --wan-prefix
--lan-ip / --lan-gw / --wan-ip / --wan-gw / --router-wan-ip
```

## 07: DNS stability benchmark

Script:

```text
run_router_bench_v4_dns_stability.py
```

Execution:

```bash
sudo python3 test_basic_speed.py \
  --target i226_1g \
  --run-dns-stability \
  --dns-stability-qps 100
```

The script performs the delayed cache-table recheck after
`--dns-table-reuse-interval-sec`, then sends unique qnames for
`--dns-nocache-load-duration-sec` at the fixed `--dns-stability-qps` value.
It does not search for a QPS value. Defaults are 600 seconds before the table
recheck, 100 QPS, and 3600 seconds of sustained load. Table-size probing also
stops only after three consecutive NG steps.

This test is excluded from the no-option default set and `--run-all`.
`--run-dns-all` selects both DNS scripts. `--run-dns-load` remains a
compatibility alias for `--run-dns-stability`.

Useful stability options:

```text
--dns-stability-qps
--dns-nocache-load-duration-sec
--dns-load-progress-interval-sec
--dns-table-reuse-interval-sec
--dns-skip-table
```

## 08: DHCPv4 basic latency using scratch raw socket

Script:

```text
run_router_bench_v4_dhcp_scratch.py
```

Purpose:

- Does not use TRex EMU.
- Uses a small Python AF_PACKET raw-socket DHCPv4 client.
- Sends DHCP DISCOVER/REQUEST/RELEASE directly as Ethernet frames from the DUT LAN-side NIC.
- Checks basic DHCP DORA success and latency.
- Runs a small burst test.
- Runs a same-MAC reconnect test.

Default behavior:

```text
single_client_latency: 1 client x 3 trials
burst_clients_latency: 10 clients x 1 trial
reconnect: same MAC initial lease -> DHCP RELEASE -> 60 sec wait -> same MAC reconnect x 1 trial
```

Each normal trial uses a different MAC address range to reduce interference from previously created DUT leases. The reconnect test intentionally reuses the same MAC before and after the 60-second wait.

Main outputs:

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

Useful launcher execution:

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

Useful direct execution:

```bash
sudo python3 tests/run_router_bench_v4_dhcp_scratch.py \
  --target i226_1g \
  --out-dir /var/trex/dhcp_scratch_test
```

Useful options:

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
--interfaces
--lan-ip / --lan-gw / --wan-ip / --wan-gw / --router-wan-ip
--line-rate-gbps
--cores
```

Notes:

- The DUT LAN-side DHCP server must be enabled.
- The DUT DHCP pool must contain enough free leases for all trial MAC addresses.
- The reported latency is discover-to-ACK time when ACK is observed.
- The reconnect test sends DHCP RELEASE, waits 60 seconds by default, and then performs DORA again with the same MAC address.
- The LAN NIC is moved into a dedicated namespace during the test and restored afterwards.



## ARP refresh and link readiness

- TRex STL/ASTF waits until every selected port reports link UP before ARP resolution or a measurement step.
- TRex is started with `--arp-refresh-period 60`, so long-running stateful tests send gratuitous ARP every 60 seconds.
- STL still performs ARP resolution, explicit MAC selection, NAT warm-up, NAT-port discovery, and bidirectional probes before every rate step.
- Namespace/raw-socket tests send a directed ARP request and a gratuitous ARP request before every phase and every 60 seconds while the test remains active.
- L4 latency, DNS, and TCP scratch tests write `arp_refresh.csv`.
- DHCP scratch sends ARP announcements from each leased client immediately after DHCP ACK.

## NIC driver state handling

TRex based tests and namespace based tests require different NIC states:

| Test group | Driver state |
|---|---|
| STL / ASTF | NICs bound to DPDK driver. |
| L4 latency / DNS | NICs bound to kernel driver and moved into Linux namespaces. |

The scripts perform the needed transition automatically when profiles define both `kernel_drivers` and `dpdk_drivers`.

`trex_interface_prepare.csv` records namespace deletion and PCI driver binding actions for TRex-based tests.

`namespace_setup.csv` records namespace creation, interface movement, and IP configuration for L4/DNS tests.


### Common test-start delay

All standalone benchmark scripts wait 15 seconds after link readiness and before the first test traffic. Override with `--test-start-delay-sec`; `0` disables the wait. Helper/server/client internal roles do not wait independently.

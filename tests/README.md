## 通信事前確認の適用範囲

各項目は、試験方式に適用できる場合だけ実装する。

| 試験 | 適用する確認 | 適用しない項目と理由 |
|---|---|---|
| STL throughput/PPS | ステップごとのARP、明示MAC、NAT外部UDPポート取得、warm NAT成功判定、上り・下り32パケット確認、方向別統計 | なし |
| ASTF max sessions / CPS | 各測定前の小規模TCP往復事前試験。接続成功と両方向L7バイトを確認 | NAT外部ポートと明示MACはASTF/TRexが動的に管理するため対象外 |
| ASTF NAT stability | 各サイクル前に同一プロトコル・同一payload directionの小規模往復事前試験 | NAT外部ポートと明示MACはASTF/TRexが動的に管理するため対象外 |
| TCP scratch max sessions | 本測定前に単一TCP接続でUP要求とDOWN応答を確認 | カーネルTCP/IPがNATポートとEthernetヘッダーを管理するため外部ポート取得・明示MACは対象外 |
| L4 latency | 各フェーズ前の既存TCP/UDP request-response preflight | カーネルソケット試験のため外部ポート取得・明示MACは対象外 |
| DNS | 各負荷ステップ前に3クエリの要求・応答確認。事前クエリ分はWAN問い合わせ数から除外 | DNS forwarder試験であり、固定NAT外部UDPポートを生成しない |
| DHCP scratch | 各試行自体がDISCOVER/OFFER/REQUEST/ACKの要求・応答確認。クライアントMACはDHCPフレームに明示済み | NAT、warm NAT、双方向負荷統計は対象外 |

# Router benchmark tests

This directory contains the individual benchmark implementations used by `test_basic_speed.py`.

The launcher is the normal entry point:

```bash
sudo python3 test_basic_speed.py --target i226_1g --duration 60
```

Individual tests can also be executed directly when debugging a specific subsystem.

## File map

| File | Role |
|---|---|
| `target_profiles.py` | Shared hardware, PCI, driver, port, and IPv4 profile definitions. |
| `routerbench_arp_refresh.py` | Raw Ethernet ARP/GARP sender and 60-second namespace refresh worker for scratch tests. |
| `run_router_bench_v4_stl_throughput_pps.py` | TRex STL IPv4 NAT throughput and PPS benchmark. |
| `run_router_bench_v4_tcp_scratch_max_sessions.py` | TCP scratch maximum concurrent session benchmark without TRex. Included in the default set and `--run-all`. |
| `run_router_bench_v4_astf_max_sessions.py` | Legacy TRex ASTF maximum concurrent session benchmark. Launcher execution requires `--run-astf-max-sessions`. |
| `run_router_bench_v4_astf_sessions_cps.py` | TRex ASTF short-session CPS benchmark. Included in the default set and `--run-all`; can also be selected with `--run-astf-sessions-cps`. |
| `run_router_bench_v4_astf_nat_stability.py` | TRex ASTF TCP/UDP NAT stability soak benchmark. |
| `run_router_bench_v4_iperf3_latency.py` | Linux namespace based TCP/UDP L4 request-response latency benchmark with optional iperf3 load. |
| `run_router_bench_v4_dns_latency.py` | DNS rate/cache/table benchmark with built-in dummy WAN-side DNS server. |
| `run_router_bench_v4_dhcp_scratch.py` | DHCPv4 basic DORA latency, burst, and same-MAC reconnect benchmark using a scratch raw-socket client. |

## Shared library responsibilities

Common implementation lives under `tests/lib/` and is imported by launchers and
benchmark scripts:

| Module | Responsibility |
|---|---|
| `pci.py` | PCI BDF normalization, sysfs device/unbind paths, driver-link lookup, and sysfs writes. `network.py` keeps strict BDF validation; `trex.py` retains its historical permissive prefixing behavior. |
| `refresh.py` | Shared endpoint logging, failure policy, CSV locking, and periodic worker lifecycle. `arp.py` and `ndp.py` retain their protocol-specific transmission and neighbor-resolution functions. |
| `launcher.py` | Shared child-option parsing/validation/filtering, launcher test metadata entries, and metadata file writing. Launcher-specific selection, metadata keys, diagnostics, and child execution policy remain in each entry point. |
| `common.py`, `repeat.py`, `output.py` | Shared timestamp/CSV helpers, repeated child execution and summary aggregation, and `details/` directory layout. CSV schemas and per-benchmark summary selections remain benchmark-specific. |
| `network.py` | Namespace, interface, and kernel-driver operations. A benchmark should use only the preparation/cleanup APIs matching its own namespace topology and restore policy. |

The refactoring intentionally leaves traffic generation, protocol checks,
measurement timing, and benchmark-specific output schemas in their existing
owners. See `REFACTORING_SPEC.md` for compatibility constraints and staged
refactoring scope.

## Launcher order

The launcher executes tests in this order:

```text
01_stl_throughput_pps
02_tcp_scratch_max_sessions
02_astf_max_sessions_legacy       # explicit selector only
03_astf_sessions_cps
04_astf_nat_stability             # --run-all or explicit selector only
05_iperf3_l4rr_latency
06_dns_forwarder_cache_latency
07_dhcp_scratch_lease_acquisition
```

No-option launcher execution runs the default set: STL throughput/PPS, TCP scratch max sessions, ASTF short-session CPS, L4 latency, DNS latency/cache/table, and DHCP scratch. It excludes ASTF NAT stability and the legacy ASTF maximum-session test. `--run-all` adds ASTF NAT stability and enables the delayed DNS table pass and sustained DNS load.

## Shared target profiles

`target_profiles.py` is the single source of target information. The main profile fields are:

| Field | Meaning |
|---|---|
| `interfaces` | PCI BDF list used by TRex/DPDK. |
| `kernel_drivers` | Kernel drivers used by namespace based tests. |
| `dpdk_drivers` | DPDK drivers used by TRex STL/ASTF tests. `auto` enables driver auto-selection. |
| `lan_iface`, `wan_iface` | Linux netdev names used by L4/DNS namespace tests. |
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

## 01: STL throughput/PPS

Script:

```text
run_router_bench_v4_stl_throughput_pps.py
```

Purpose:

- Generates IPv4 NAT traffic with TRex STL.
- Measures throughput and PPS for 64-byte and large-frame traffic.
- Performs Mbps-based rate search by default.
- Writes NDR/PDR summary rows.
- Before every rate step, refreshes ARP on both ports and writes explicit source/destination MAC addresses into every generated frame.
- Before every rate step, sends warm-up NAT traffic, captures the WAN-side translated UDP source port, and uses that port for downlink traffic.
- Before every rate step, verifies 32 uplink packets and 32 downlink packets. A failed preflight prevents that measurement step from starting.
- Bidirectional rows include combined counters and independent `up_*` / `down_*` counters and loss rates.

Main outputs:

```text
summary_all.csv
stl_raw.csv
stl_preflight.csv
metadata.json
trex_cfg.yaml
trex_interface_prepare.csv
trex_server_stl.log
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

Delayed table pass: the second `cache_table_size` pass waits `--dns-table-reuse-interval-sec` seconds, 600 seconds by default. It is skipped unless `--dns-run-delayed-table` is specified. The launcher supplies this flag for `--run-all` and `--run-dns-all`; plain `--run-dns-latency` omits it.
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

- `tcp_scratch_max_sessions_timeseries.csv`
- `tcp_scratch_max_sessions_sessions.csv`
- `tcp_scratch_server_timeseries.csv`
  - Includes `target_session_attempts_total` and `target_session_attempts_delta`, copied from the LAN-side session creation status so WAN accept counts can be compared with attempted connection counts.
- `tcp_scratch_max_sessions_summary.json`
- `tcp_scratch_server_summary.json`
- `summary.csv`
- `summary.json`

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
astf_raw.csv
astf_max_sessions_timeseries.csv
metadata.json
trex_server_astf.log
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
astf_raw.csv
metadata.json
trex_server_astf.log
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
summary_tcp.csv
summary_udp.csv
astf_nat_stability_tcp_cycles.csv
astf_nat_stability_tcp_raw.csv
astf_nat_stability_udp_cycles.csv
astf_nat_stability_udp_raw.csv
metadata.json
trex_server_astf.log
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
latency_idle_samples.csv
latency_load_10pct_samples.csv ... latency_load_99pct_samples.csv
l4rr_client_idle.json
l4rr_client_load_10pct.json ... l4rr_client_load_99pct.json
iperf3_client_load_10pct.json ... iperf3_client_load_99pct.json
metadata.json
namespace_setup.csv
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

## 06: DNS rate/cache/table benchmark

Script:

```text
run_router_bench_v4_dns_latency.py
```

Purpose:

- Starts a built-in dummy UDP DNS server in the WAN namespace.
- Sends DNS A queries from the LAN namespace to the DUT DNS forwarder.
- Measures non-cache DNS forwarding performance at E6 QPS steps from 10 to 10000.
- Measures cache-hit DNS response performance at cache E6 QPS steps from 10 to 48000.
- Measures approximate DNS cache table size by checking whether re-queries reach the WAN DNS server.

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
  E6 table-size steps from 10 to 10000 use an independent qname range.
  Each larger step adds only the missing qnames, then re-queries the whole accumulated pool at 100 QPS.
  Only generated benchmark qnames are included in wan_queries_delta; unrelated router background DNS traffic is excluded.
  The subtest stops when failure_rate_pct >= 50 or wan_queries_delta / cache_table_size >= 50%.

cache_table_size:
  Optional delayed second pass, enabled by --dns-run-delayed-table.
  It waits 600 seconds by default, then uses a second independent accumulated qname range.
  Each table-size step preloads the exact qname set and immediately verifies the same set at 100 QPS.
  The subtest stops when failure_rate_pct >= 50 or wan_queries_delta / cache_table_size >= 50%.

nocache_rate:
  E6 QPS steps: 10,15,22,33,47,68,100,150,220,330,470,680,1000,1500,2200,3300,4700,6800,10000
  Unique qnames are queried for each request.
  Each QPS step uses --duration seconds.
  The subtest stops when failure_rate_pct >= 50 or rtt_max_ms >= 1000.

cache_rate:
  First, 100 qnames are preloaded into the DUT DNS cache.
  The same 100 qnames are then queried in round-robin order at cache E6 QPS steps from 10 to 48000.
  Each QPS step uses --duration seconds.
  The subtest stops when failure_rate_pct >= 50 or rtt_max_ms >= 1000.
```

Main outputs:

```text
summary_all.csv
dns_server_queries.csv                 server-side failures only; minimal columns
dns_server_queries.csv.stats.json      all query counters and failure counters
dns_<phase>_<stage>_<step>_samples.csv
dns_<phase>_<stage>_<step>.json
metadata.json
namespace_setup.csv
dns_server.stdout.log
dns_server.stderr.log
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
--dns-table-reuse-interval-sec
--dns-run-delayed-table
--dns-skip-table-reuse
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

## 07: DHCPv4 basic latency using scratch raw socket

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
- STL still performs ARP resolution, explicit MAC selection, NAT warm-up, NAT-port discovery, and bidirectional preflight probes before every rate step. It runs one preflight by default; `--stl-recovery-probe` enables retries every two seconds until three consecutive checks succeed.
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

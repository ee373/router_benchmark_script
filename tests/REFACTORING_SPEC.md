# Router Benchmark 共通処理リファクタリング仕様書

## 1. 目的

ベンチマークの機能追加に伴って複数の実装に重複した処理を `tests/lib` に集約し、変更箇所と保守コストを減らす。一方、既存のベンチマーク仕様、ランチャーの操作性、出力形式、測定条件は維持する。

本書は、現行コードを前提にしたリファクタリングの提案仕様である。実装時に既存の挙動と異なる点が見つかった場合は、測定仕様を独断で変更せず、差異と影響を明示して判断する。

## 2. 適用範囲

### 2.1 対象

- 共通処理の配置先は既存の `tests/lib/` とする。
- 対象はランチャーと `tests/run_router_bench_*.py` の間で共有できる処理、および既存の `tests/lib` 内で重複している低レベル処理とする。
- 現行のユニットテストをすべて残し、必要なテストを追加する。
- 共通化により既存の公開関数・CLI・ファイル出力に影響する場合は、互換性を保つ薄い委譲関数を残す。

### 2.2 対象外

- ベンチマークの負荷、レート探索、タイムアウト、リトライ、測定時間、試験順序の変更。
- NAT、DNS、DHCP、ASTF、STL、IPv6、DS-Lite、MAP-Eなど、試験方式に固有のパケット生成・判定・統計計算の一律な統合。
- 設定値やハードウェアプロファイルの意味の変更。
- 本番環境へのデプロイ、NIC・ルーティング・権限設定の変更。
- 結果ディレクトリや過去の結果ファイルの削除・上書き。

## 3. 現行構成と既存資産

### 3.1 既存の共通ライブラリ

| モジュール | 現在の責務 | 方針 |
|---|---|---|
| `tests/lib/config.py` | JSON設定、共通デフォルト、ターゲットプロファイルの読込・継承・IPバージョン変換 | 既存APIと設定ファイルを維持し、各ベンチマーク独自の設定読込を追加で重複させない。 |
| `tests/lib/common.py` | 時刻、数値引数の基本パース、構造データの平坦化、CSV/JSONセル化、共通統計・summary CSV | 汎用処理の既存APIを継続利用する。 |
| `tests/lib/output.py` | `details/` 作成 | 結果ディレクトリの互換性を維持する。 |
| `tests/lib/repeat.py` | 別プロセスでの反復実行、反復メタデータ、CSV集約 | 子プロセス実行と出力配置を維持する。 |
| `tests/lib/rate_steps.py` | E12レート生成、上限適用、高損失連続回数、表示用整形 | 既存の境界条件を維持する。 |
| `tests/lib/source_ports.py` | LAN送信元ポート範囲のパース・表示・CLI登録 | 既存の表記形式・検証範囲を維持する。 |
| `tests/lib/preflight.py` | 任意リカバリ確認、連続成功ゲート、記録CSV、CLI引数 | 既存の成功回数・間隔・タイムアウト・例外動作を維持する。 |
| `tests/lib/timing.py` | リンク確立後の開始待ち | 既存の設定値、環境変数、ログ、待ち時間を維持する。 |
| `tests/lib/trex.py` | TRex設定、プロセス・RPC・ポート準備、PCI/DPDK関連操作 | TRex固有のライフサイクルを維持する。低レベルPCI重複だけを整理する。 |
| `tests/lib/astf.py` | IPv4 NAT ASTF共通処理、プロファイル・進捗・TRex ASTFサーバー | ASTF特有の通信・測定意味を維持する。 |
| `tests/lib/network.py` | 名前空間、Linuxインターフェース、kernel driver、PCI準備など | 名前空間・kernel driverの責務を維持し、重複する低レベル処理を整理する。 |
| `tests/lib/arp.py`, `tests/lib/ndp.py` | IPv4 ARP / IPv6 NDPの解決・定期更新 | プロトコル固有の処理は維持し、共通の更新ライフサイクルのみ統合候補とする。 |
| `tests/lib/pci.py` | PCIデバイスパスからの現在driver取得 | `network.py` と `trex.py` の共通低レベル処理の集約先候補とする。 |
| `tests/lib/dslite.py`, `tests/lib/mape.py` | 各方式のプロファイル検証、TRex target変換、レート計算等 | 方式固有の検証・計算を共通化しない。 |

ライブラリは既に複数存在するため、新たなトップレベル `lib/` を作らない。新規共有処理は原則として既存モジュールへ追加し、責務が明確に独立し、複数の呼び出し元がある場合に限って新モジュールを検討する。

### 3.2 既存テスト

`unit_tests/` 以下のファイルを削除・改名せず維持する。主な既存カバレッジは以下の通り。

| テスト | 現在の対象 |
|---|---|
| `test_shared_config.py` | 共通設定、TRex設定生成、IPv6 gateway MAC等 |
| `test_dslite_config.py`, `test_mape_config.py` | 方式別設定・profile検証 |
| `test_rate_steps.py` | E12ステップとレート関連関数 |
| `test_source_ports.py` | 送信元ポート範囲 |
| `test_preflight.py` | 連続preflight、launcherからのオプション転送 |
| `test_output_layout.py` | `details/`、repeat実行・集約、TCP scratch時系列出力 |
| `test_astf_nat_stability_profile.py` | ASTF NAT stability profile |
| `test_dslite_source_port_scan.py` | DS-Lite送信元ポート走査 |

現在のカバレッジがある機能も、統合後に旧テストを置き換えずに実行し、新API用の境界・異常系テストを追加する。

## 4. 共通化対象と責務

優先度は、低リスクで重複が明確なものから着手する順である。

### P1: PCI低レベル共通処理

**現状の候補:** `tests/lib/network.py` と `tests/lib/trex.py` のPCI BDF正規化、sysfs device path、現在driver取得、unbindの基礎処理。`tests/lib/pci.py` は現在driver取得の共通化を開始済み。

**仕様:**

- PCIの表記検証・正規化、sysfs device pathの組立、driver linkの読取など、OS/PCI共通の低レベル処理を `tests/lib/pci.py` に集約する。
- kernel driverとDPDK driverのバインド・復元フロー、必要/任意エラーの扱い、ログイベントはそれぞれ `network.py` / `trex.py` 側に残す。
- 既存関数を外部から利用している箇所がある場合、旧関数名と戻り値の意味を維持して内部から共通関数へ委譲する。
- 不正なBDFを空文字にするかエラーとするかなど、現在の `network.py` / `trex.py` 間の挙動差は呼び出し元単位で維持する。共通化を理由に一律変更しない。
- 既存の `pci.py` のdriver link未接続時の空文字返却と、sysfs参照エラーからの回復動作を維持する。

**追加テスト:**

- 2桁domain省略形、4桁domain形式、不正形式、空値。
- sysfs driver linkあり／なし、参照不能時の戻り値。
- network側とTRex側の既存エラー・ログ・最終driver検証の互換性。
- OS依存sysfsアクセスは一時パスまたはmockで検証し、実機操作を単体テストで行わない。

### P2: ARP/NDP定期更新のライフサイクル

**現状の候補:** `tests/lib/arp.py` と `tests/lib/ndp.py` の refresherにあるendpoint保持、thread/event管理、周期実行、排他付きCSV追記、開始・停止・context manager。

**仕様:**

- 共通化する場合も、ARPフレーム生成・送信とIPv6 ping/neighbor確認はプロトコル固有の関数として分離する。
- endpointごとの処理失敗を記録し、`required=True` の場合のみ呼び出し元へ失敗を通知する現行契約を維持する。
- 即時更新、既定周期、`periodic_60s` 理由、CSV列、時刻形式、警告出力、停止時のjoin上限を維持する。
- daemon threadを使う場合も、二重start防止、停止後の挙動、更新中の停止を明示的にテストする。
- 共通化のためだけに設定可能な抽象基底クラスや多数のhookを追加しない。二実装で実際に共有できる最小の内部helperにとどめる。

**追加テスト:**

- 即時・周期更新の呼出し、startの重複、stop後のworker停止。
- 複数endpointの成功／一部失敗、required時の例外、CSV headerの一度だけの出力。
- ARP/NDP各プロトコル処理が従来の専用関数へ委譲されること。
- 実際のsocket送信、ping、neighbor操作はmockする。

### P3: ランチャー共通処理

**現状の候補:** `test_basic_speed.py`、`test_dslite_speed.py`、`test_mape_speed.py` には、デフォルト出力先、TEST_PLAN、child `--help` からのオプション取得、未知・余剰引数検証、子向け引数フィルタリング、launcher metadata出力などがある。基本ランチャーにはオプションキャッシュ等の独自差もある。

**仕様:**

- 共通化候補は子オプション抽出・検証・フィルタリング、launcher metadataの共通項目、共通引数の変換など純粋処理から始める。
- test plan、selectorの意味、各方式固有の引数、出力ディレクトリ接頭辞、`benchmark_mode`、ip-version情報は各ランチャーが明示して渡す。
- ランチャー間にあるエラー文、オプションキャッシュ、位置引数・`--option=value` の扱い、値引数が `-` で始まる場合の扱いを先に比較し、互換性を落とさない。
- metadataは現行キーと階層を保つ。方式により存在しない項目を成功形のデフォルト値で埋めない。
- 子プロセス実行順、失敗後の継続方針、最終終了コード、dry-run表示は変更しない。

**追加テスト:**

- 全3ランチャーの同等な共通引数・オプション検出ケース。
- 対応済み／未知オプション、`--x=value`、値付きオプション、余剰引数。
- metadataの既存キー、方式別差分、out-dirとdetails配下のパス。
- `--help`、`--dry-run`、各selectorのテスト選択順と子コマンド引数。

### P4: ネットワーク名前空間・NIC準備

**現状:** `tests/lib/network.py` に名前空間、PCI/kernel driver、netdev解決、名前空間interface操作等が集約されている。一部ベンチマークには個別準備・復元・CSV記録が残る。

**仕様:**

- まず各実装の操作順を列挙し、重複している準備・復元操作だけを既存 `network.py` のAPIへ接続する。
- interfaceの所有namespace、事前状態、bind設定、復元方針をテスト固有に指定できること。隠れた暗黙状態を追加しない。
- 明示的なcleanupは、既存の`keep`・自動bind/restore等のCLI選択を尊重する。
- namespace失敗、interface未発見、PCI driver bind失敗は呼び出し元の既存エラー経路で可視化し、広い例外catchや黙殺を増やさない。
- DHCP scratchのraw socket namespace、DNS/L4のLAN/WAN二namespace、TCP scratchなどの異なるトポロジーを、同じ操作列と仮定しない。

**追加テスト:**

- namespace/interfaceの解決、準備、既存状態、cleanup・復元判断をmockで確認。
- 部分準備失敗時に、復元・ログ・終了状態が従来どおりであること。
- 実ネットワーク操作はユニットテストから分離し、必要時のみ開発ホストで限定検証する。

### P5: CSV・metadata・反復実行の補完

**現状:** summary CSV、raw stats、反復実行、`details/`配置は一部共通化済み。各ベンチマークのCSVにはそれぞれの列・記録タイミング・互換aliasがある。

**仕様:**

- 既存の `common.py` と `repeat.py` を再利用する。共通処理が存在する場合に、同じ目的の独自writerを新設しない。
- CSV schema、列順、時刻精度、header、ファイル名、親／details配下、repeat列、summary aliasを維持する。
- ファイルごとに列・集約意味が異なるため、異なるschemaのwriterを一つの可変汎用クラスへまとめない。
- 「summary」として反復集約するファイルは、既存の対象ファイル指定と同じ意味を維持する。TCP scratchの時系列ファイルの扱いも維持する。

**追加テスト:**

- CSV列、header、複数row、空入力、特殊値。
- repeat=1の非dispatch、repeat>1の子引数、失敗時のreturn code、部分結果、集約ファイル。
- ファイル名・配置・列の互換性。ファイル内容の既存consumerがある場合はその読込みも確認する。

## 5. 共通化しない機能固有処理

以下は似た用語・構造があっても、測定の意味が異なるため原則として各ベンチマークまたは方式別ライブラリに残す。

| 機能群 | 残す処理の例 |
|---|---|
| STL throughput/PPS | ARP解決、明示MAC、NAT warm-up、外部UDPポート検出、方向別32 packet preflight、rate/PPS探索 |
| ASTF max sessions/CPS/stability | ASTF profile、session成功率、生成/teardown、payload方向、cycle条件、ASTF固有統計 |
| TCP scratch | raw/kernel TCP接続とkeepalive、session ramp、server accept時系列、ポート割当 |
| L4 latency | TCP/UDP request-response、iperf3負荷、load phase・方向、RTT集計 |
| DNS | DNS wire形式、qname割当、preflightのWAN観測、cache/table判定、query log制限、応答判定 |
| DHCP scratch / EMU | DORA、lease・同一MAC reconnect、raw socketやEMU固有処理 |
| IPv6 stability | IPv6 packet/ND、IPv6 routing向けの測定・回復判定 |
| DS-Lite / MAP-E | tunnel/NAT profile、方式固有のMTU・port計算と検証 |

プロトコルをまたぐ共通preflight枠が必要になった場合でも、判定関数、パケット、統計の定義は各方式が所有し、共通側は明示したcallableの反復・記録などに限る。既存 `preflight.py` の契約を先に調査し、二重実装を作らない。

## 6. 互換性要件

リファクタリング前後で、以下を維持する。

1. **CLI:** 既存の引数名・既定値・省略可能性・検証範囲・launcher selector・子プロセス転送。
2. **選択・順序:** launcherのデフォルト、`--run-all`、個別selector、方式ごとのTEST_PLAN順。
3. **測定:** 試験条件、トラフィック、回復判定、待ち時間、stop条件、リトライ条件。
4. **失敗意味:** fatal/non-fatal、継続方針、終了コード、ログ・CSV記録、cleanup。
5. **出力:** ディレクトリ、ファイル名、CSV列と値、JSON metadataの既存キー、反復集約。
6. **実行方法:** launcher経由と直接実行、パッケージimportと既存のnamespace実行方式。
7. **設定:** `tests/config/*.json` の既存構造・値と環境変数の意味。
8. **運用:** 実測中の追加処理・CPU負荷を増やさない。測定サイクル内の余分なpreflightやログI/Oを追加しない。

互換性を維持できない変更が必要な場合は、別の仕様変更として理由、影響するCLI・ファイル・測定値、移行方法を明記し、承認を得るまで既定動作を変更しない。

## 7. 実装手順

### Phase 0: 基準の確定

- 作業開始時の差分を把握し、無関係な変更を保持する。
- 現在のユニットテストを実行して基準結果を保存する。
- 各対象の呼び出し元、CLI、ログ、出力列、失敗動作を検索して一覧化する。
- `--help` と主要launcherの `--dry-run` 出力を基準として保存・比較できるようにする。

### Phase 1: 低リスクのPCI共通化

- `pci.py` の既存driver-link helperを基点に、`network.py` / `trex.py` の重複低レベル操作を共通化する。
- 既存APIの薄いwrapperを残し、低レベル単体テストを追加する。
- kernel/DPDK bindフローは変更せず、既存ユニットテストをすべて実行する。

### Phase 2: ARP/NDPライフサイクル

- 現行のログ列、周期、スレッド、required失敗動作を固定するテストを追加する。
- 共通化したライフサイクルへ移行し、ARPとNDPの専用通信関数は維持する。
- 長時間実測中の追加probeを行わず、mockテストでthreadとログを検証する。

### Phase 3: ランチャー純粋処理

- 3ランチャーの同種処理と差分をテストで可視化する。
- 共通化APIを導入し、各ランチャー固有値は引数として渡す。
- CLI/metadata/plan順/子引数の比較テストを通してから次の整理に進む。

### Phase 4: 個別ネットワーク準備・出力の段階移行

- 1機能群ずつ `network.py` / `common.py` / `repeat.py` 等への委譲を進める。
- 1つのベンチマーク変更につき既存出力・失敗・cleanupテストを追加する。
- 大きな一括置換は避け、同じ共通処理を使う複数呼び出し元を一単位として変更する。

### Phase 5: 開発環境の限定確認

- ローカルまたは開発環境で全既存ユニットテストを実行する。
- 変更範囲に関係する `--help`、`--dry-run`、syntax/import確認を行う。
- NIC/namespace/driverに影響する変更がある場合、必要な範囲の開発環境確認だけを行う。
- 実ベンチマークはユーザー依頼された範囲に限り、同時実行しない。
- 成功した確認、実施できなかった確認、残課題を区別して記録する。

## 8. テスト要件と完了条件

### 8.1 必須テスト

- 既存の `unit_tests/` の全テストを削除せず、リファクタリング後も通過する。
- 共通APIの正常系、境界値、異常系、必要な副作用（CSV/metadata/ログ）をユニットテストする。
- 呼び出し元側に互換性テストを置き、旧API委譲・CLI・出力契約を検証する。
- 対象となるlauncherは `--help` と `--dry-run` の成功を確認し、選択順と転送引数を比較する。
- ネットワーク、TRex、root権限、実機NICに依存する箇所は単体テストで直接操作せずmockする。

### 8.2 完了条件

- 全既存ユニットテストが成功する。
- 追加テストが共通化コードと互換境界の両方を検証している。
- 対象範囲のCLI、出力ファイル名、列・metadata、終了コードに意図しない差がない。
- 機能固有の測定条件を共通化によって変更していない。
- ソース・設定・テストに不要な変更がなく、結果・差分・未検証事項を報告できる。

## 9. 実装時の設計規約

- 共有コードの依存方向は「ランチャー／ベンチマーク → `tests/lib` → Python標準ライブラリ」とし、`tests/lib` 間の循環importを作らない。
- CLI・ファイルシステム・OS操作と、純粋な変換・検証処理を分け、可能な限り純粋関数としてテストする。
- エラーは具体的な例外または既存のエラー経路で利用者に見せる。広い例外捕捉、無言の既定値、成功扱いの代替値を追加しない。
- 既存の関数名やimportが外部・テストから使われている場合、明示的な移行理由がない限りwrapperを残す。
- 型・戻り値・副作用を明確にし、`Any`相当の曖昧な汎用化を避ける。
- コメントは、プロトコル差や互換性上の理由など、コードのみでは分かりにくい箇所に限る。

## 10. 成果物

- 共通処理を段階移行したPythonソース。
- 既存テストを保持しつつ追加したユニット・互換性テスト。
- 本仕様の適用状況と変更後の使い方を反映した `tests/README.md`。
- 検証コマンド、結果、未検証項目、既知の差異の実施報告。

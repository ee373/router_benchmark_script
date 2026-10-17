# RouterBench Development Rules

## 1. Environment

- Local project: `C:\work\dev\router_benchmark_script`
- Local results: `C:\work\dev\router_benchmark_script\results`
- SSH host: `Trex-Iptester` (`iptester@172.20.10.100`)
- Remote development: `/opt/routerbench_dev`
- Remote production: `/opt/routerbench`
- TRex installation: `/opt/trex/v3.08`
- Remote results: `/var/trex`
- Use PowerShell, Windows OpenSSH (`ssh`, `scp`), and Git.

## 2. Source Management

- The Windows project is the authoritative source.
- Edit source files only on Windows. Deploy changes using SCP.
- Use `/opt/routerbench_dev` for all development and verification.
- Never edit remote source files directly except through SCP deployment.
- Preserve existing benchmark specifications and shared libraries unless changes are required.
- Manage Git on Windows. Do not commit without user instruction.
- Exclude `/results/` from Git tracking.

## 3. Development and Verification

For development, debugging, unit tests, integration tests, and hardware verification, use `/opt/routerbench_dev` by default.

1. Modify the local source code.
2. Deploy changed files and dependencies using SCP.
3. Execute verification through SSH.
4. Download results to the local `results` directory.
5. Analyze failures, correct the local source, and repeat verification as necessary.

Routine deployment, verification, and result collection may run automatically without individual approval.

Use existing benchmark launchers and supported arguments. Do not modify sudoers, file permissions, NIC configuration, routing, or other system settings without explicit authorization.

### SCP deployment examples

```powershell
# Deploy a launcher
scp "C:\work\dev\router_benchmark_script\test_dslite_speed.py" "Trex-Iptester:/opt/routerbench_dev/"

# Deploy a shared library
scp "C:\work\dev\router_benchmark_script\tests\lib\preflight.py" "Trex-Iptester:/opt/routerbench_dev/tests/lib/"

# Deploy a configuration file
scp "C:\work\dev\router_benchmark_script\tests\config\benchmark_defaults.json" "Trex-Iptester:/opt/routerbench_dev/tests/config/"
```

### Verification command examples

Use absolute script paths with `sudo -n`.

```powershell
# Python syntax verification
ssh Trex-Iptester "cd /opt/routerbench_dev && python3 -m py_compile test_dslite_speed.py"

# Unit tests
ssh Trex-Iptester "cd /opt/routerbench_dev && python3 -m unittest discover -s unit_tests -v"

# DS-Lite benchmark
ssh Trex-Iptester "cd /opt/routerbench_dev && sudo -n /usr/bin/python3 /opt/routerbench_dev/test_dslite_speed.py --target i226_1g_dslite --duration 20 --repeat 1"
```

The benchmark launchers are:

- `test_basic_speed.py`
- `test_dslite_speed.py`
- `test_mape_speed.py`

Execute all launchers using the same absolute-path convention.

If `sudo -n` fails, inspect the existing sudoers permissions rather than attempting password entry or automatic privilege changes.

## 4. Production Deployment

- `/opt/routerbench` is the production environment.
- Deploy to production only when the user explicitly requests production deployment.
- Successful development verification does not authorize production deployment.
- Do not execute development tests or debugging operations in production.
- Production deployment must not overwrite unrelated files or configurations.

### Production deployment examples

The following commands are permitted only after an explicit production deployment request.

```powershell
# Deploy a verified launcher to production
scp "C:\work\dev\router_benchmark_script\test_dslite_speed.py" "Trex-Iptester:/opt/routerbench/"

# Execute a production benchmark when explicitly requested
ssh Trex-Iptester "cd /opt/routerbench && sudo -n /usr/bin/python3 /opt/routerbench/test_dslite_speed.py --target i226_1g_dslite --duration 20 --repeat 1"
```

## 5. Benchmark Execution

- Never execute multiple benchmarks concurrently, including across development and production.
- Never deploy or modify files during an active benchmark.
- Do not interrupt or restart active benchmarks without authorization.
- Avoid CPU-intensive development operations on the TRex machine during measurements.
- Do not perform intermediate Preflight recovery checks during active ASTF NAT stability measurements, including basic NAT and DS-Lite.
- Preserve existing measurement conditions and recovery behavior.
- Limit verification to the requested scope. Do not initiate unrelated or lengthy benchmarks.

## 6. Results and Reporting

- Download results under `C:\work\dev\router_benchmark_script\results`.
- Use explicit local SCP destination paths.
- Preserve individual result directories and exclude `/results/` from Git.
- Do not delete or overwrite existing results.
- Associate results with the deployed Git revision and file hashes when appropriate.
- Report modified files, verification performed, results, and unresolved issues.

### Result download example

```powershell
# Create the result directory
New-Item -ItemType Directory -Force "C:\work\dev\router_benchmark_script\results"

# Download benchmark results
scp -r "Trex-Iptester:/var/trex/trex_dslite_speed_i226_1g_dslite_20261012_185709" "C:\work\dev\router_benchmark_script\results\"
```

Use the actual remote result directory generated by each benchmark. Do not assume the example directory name applies to subsequent executions.

Verify benchmark results rather than relying solely on process exit status. Distinguish completed verification from untested changes.
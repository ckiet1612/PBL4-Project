# Environment inventory

Inventory time: **2026-09-17T02:15:54+07:00**. Labels: `observed` means a read-only command on the current machine; `user-provided` means stated in approved PLAN/task but not observed; `unconfirmed` means no evidence. Inventory labels are not acceptance statuses.

No secret was read, no remote server contacted, no deployment/tool installed or started.

## Current development machine

| Item | Value | Label | Source |
|---|---|---|---|
| OS | macOS 27.0, build 26A428; Darwin 27.0.0 | observed | `sw_vers`, `uname -a` |
| Architecture/chip | arm64, Apple M1 | observed | `uname -m`, filtered `system_profiler SPHardwareDataType -json` |
| CPU/RAM | 8 cores (`proc 8:0:4:4`), 8 GB | observed | filtered `system_profiler`; sandboxed `sysctl` was denied and is not used as evidence |
| Repository filesystem | `/System/Volumes/Data`, 460 GiB total, 241 GiB used, 187 GiB available at observation | observed | `df -h .`; transient capacity only |
| Linux/cgroups v2 | Not present on this macOS host | observed | `/sys/fs/cgroup/cgroup.controllers` read check failed as expected |
| NVIDIA GPU/driver/toolkit | `nvidia-smi` not installed; Apple M1 is not an NVIDIA acceptance host | observed | command availability; no GPU claim |

macOS is suitable for documentation/development/simulator only. It cannot satisfy Linux/cgroups/container isolation, portability, reboot/load/soak or NVIDIA gates.

## Tool inventory

| Tool | Observed version/state | Label | B02/readiness impact |
|---|---|---|---|
| Git | 2.50.1 (Apple Git-155) | observed | Available |
| Python | CPython 3.12.13 via `python3` and `python3.12` | observed | Meets Python 3.12 floor |
| `uv` | Command not found | observed | Must be provisioned before B02 dependency/bootstrap commands; B01 not blocked |
| Docker CLI | 29.5.3 build d1c06ef | observed | CLI present; daemon and Linux isolation not validated |
| Docker Compose | v5.1.4 | observed | Plugin present; deployment not started |
| PostgreSQL client | `psql` command not found | observed | Must be provisioned before DB integration/admin diagnostics |
| Node.js | v26.4.0 | observed | Installed; LTS/support alignment for B02 must be confirmed, not assumed |
| pnpm | 11.9.0 | observed | Installed; B02 locks supported version/toolchain |
| Ruby/Psych | system Ruby 2.6 with YAML parser | observed during B01 | Used only for local YAML parse; not a product dependency |
| Python schema packages | PyYAML/jsonschema/openapi-spec-validator/referencing absent in current Python | observed | B01 may use isolated temporary validators; no product dependency added |

## Required Linux, portability and GPU environments

| Capability | Required source | Availability | Affected gates / latest need |
|---|---|---|---|
| Reference Linux benchmark host, Ubuntu 24.04 or equivalent, cgroups v2, ≥8 vCPU/16 GiB, SSD, persistent DB/artifact, non-root user/time sync/firewall | PLAN §10 | unconfirmed | ACC-13,16–17,19–26,29–32,35–39; required before relevant B20–B25 evidence, earlier for B09/B15 integration |
| Second different Linux configuration, independent deployment, same release artifacts | PLAN §4/§14 | unconfirmed | ACC-32–33,37–38; must be identified before B21 execution |
| Storage durability/fsync/backup target characteristics | PLAN §2/§9/§10 | unconfirmed | ACC-17,20,23,28,31,33; B07/B21 test design needs actual filesystem/volume |
| Published `linux/amd64` image build/test host | PLAN §4/§14 if architecture claimed | unconfirmed | ACC-32/37; only claim architecture actually built/tested |
| Published `linux/arm64` image build/test host | PLAN §4/§14 if architecture claimed | unconfirmed | ACC-32/37; current macOS arm64 is not Linux image evidence |
| NVIDIA GPU + compatible driver, Container Toolkit, Docker runtime, framework/CUDA | PLAN §10/§14 conditional | unconfirmed | ACC-34 and GPU portion of ACC-04/18/19/25/35/38; B23 can report unverified if absent |

Missing Linux/GPU does not block B01 contract or CPU-core implementation. It blocks only the corresponding evidence/claim and must never be converted to `pass` by simulator/documentation.

## CI, registry and repository access

| Item | Inventory | Label |
|---|---|---|
| Git remote | `origin` points to `https://github.com/ckiet1612/PBL4-Project.git`; local `main` and `origin/main` were reported at `ac8a0e5` before B01 | observed from Git/thread context |
| GitHub authentication/branch protection | No credential/rule inspection performed | unconfirmed |
| GitHub Actions execution access | No workflow exists yet and permissions were not tested | unconfirmed |
| GHCR push/pull/signing/provenance access | Not tested; no credential read | unconfirmed |
| Release permission | Not granted to B01; release belongs to B25 | user-provided scope |

B02 should confirm GitHub/registry permissions without printing tokens and create CI only after contract lock. B25 separately needs explicit publish authority.

## Personnel and ownership

No named maintainer, benchmark operator, Linux host owner, security reviewer or release operator was provided. This is `unconfirmed`, not evidence of absence. Before B20–B25, project coordination must assign people for long-running load/soak, two-host portability/relocation, GPU conditional testing, security review and release publication. No personal identity is invented in ADR/evidence.

## Inventory follow-up gates

| Before task | Required confirmation |
|---|---|
| B02 | `uv`, supported Node LTS/pnpm policy, PostgreSQL client or containerized equivalent, GitHub Actions permissions |
| B09/B15 | Linux/cgroups v2 Docker host and storage behavior for executor/deadline/recovery tests |
| B21 | Two independent Linux configurations, backup destination/filesystem semantics, supported image architectures |
| B22 | Reference load host capacity, exclusive benchmark window, time sync and operator for ≥3 runs/8 h soak |
| B23 | Real NVIDIA availability/driver/Container Toolkit; otherwise preserve “GPU simulated/unverified” wording |
| B25 | GHCR/GitHub release/signing permissions and clean-host operator |

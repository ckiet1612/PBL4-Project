# Environment inventory

Inventory refreshed: **2026-09-18T01:30:43+07:00**. Labels: `observed` means a command was run on the current machine; `user-provided` means stated in approved PLAN/task/design but not observed; `unverified` means no direct evidence; `blocked` means a named missing prerequisite prevents the stated check. Inventory labels are not acceptance statuses.

No secret was read and no deployment was started. Public release metadata supplied isolated uv 0.12.15, Node 24.21.0 LTS and actionlint 1.7.12 under `/tmp`; archives/binaries were checksum-verified where published. No global tool or machine configuration was changed. `uv sync` and `pnpm install` populated ignored `.venv/` and `web/node_modules/`.

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
| `uv` on system PATH | Command not found; common existing binary locations also absent | observed | Normal contributor prerequisite remains external to the repository |
| Isolated `uv` | 0.12.15, arm64 macOS wheel | observed | Generated/checked `uv.lock`, frozen-synced Python 3.12 and ran Ruff/pytest successfully |
| Docker CLI | 29.5.3 build d1c06ef | observed | CLI present; daemon and Linux isolation not validated |
| Docker Compose | v5.1.4 | observed | Plugin present; deployment not started |
| PostgreSQL client | `psql` command not found | observed | Must be provisioned before DB integration/admin diagnostics |
| Node.js | v26.4.0 | observed | Used for local UI verification only; not the selected support target |
| Node.js target | v24.21.0 LTS (Krypton), checksum-verified arm64 archive | observed | Frozen install, typecheck and Vite build passed; child commands confirmed Node 24 |
| pnpm | 11.9.0 | observed | Generated `web/pnpm-lock.yaml`; frozen reinstall preserved its checksum |
| UI dependencies | React/React DOM 19.3.0, TypeScript 7.0.2, Vite 8.3.0, Vite React plugin 6.1.1 | observed | Registry versions resolved by pnpm; typecheck and build passed locally |
| Pytest | 9.1.1 from `uv.lock` | observed | Full frozen suite passed |
| Ruff | 0.16.8 from `uv.lock` | observed | Full frozen lint and format checks passed |
| actionlint | 1.7.12, checksum-verified arm64 release | observed | `.github/workflows/ci.yml` passed semantic validation |
| Ruby/Psych | system Ruby 2.6 with YAML parser | observed during B01 | Used only for local YAML parse; not a product dependency |
| Python schema packages | PyYAML/jsonschema/openapi-spec-validator/referencing absent in current Python | observed | B01 may use isolated temporary validators; no product dependency added |

## B02 prerequisite closure

`uv.lock`, frozen Python install/quality checks, Node 24 LTS web checks and CI syntax/semantics are verified. No direct B02 blocker remains. Missing `psql`, Linux, PostgreSQL, Docker daemon and GPU evidence belong to later tasks and are not B02 blockers.

## Required Linux, portability and GPU environments

| Capability | Required source | Availability | Affected gates / latest need |
|---|---|---|---|
| Reference Linux benchmark host, Ubuntu 24.04 or equivalent, cgroups v2, ≥8 vCPU/16 GiB, SSD, persistent DB/artifact, non-root user/time sync/firewall | PLAN §10 | unverified | ACC-13,16–17,19–26,29–32,35–39; required before relevant B20–B25 evidence, earlier for B09/B15 integration |
| Second different Linux configuration, independent deployment, same release artifacts | PLAN §4/§14 | unverified | ACC-32–33,37–38; must be identified before B21 execution |
| Storage durability/fsync/backup target characteristics | PLAN §2/§9/§10 | unverified | ACC-17,20,23,28,31,33; B07/B21 test design needs actual filesystem/volume |
| Published `linux/amd64` image build/test host | PLAN §4/§14 if architecture claimed | unverified | ACC-32/37; only claim architecture actually built/tested |
| Published `linux/arm64` image build/test host | PLAN §4/§14 if architecture claimed | unverified | ACC-32/37; current macOS arm64 is not Linux image evidence |
| NVIDIA GPU + compatible driver, Container Toolkit, Docker runtime, framework/CUDA | PLAN §10/§14 conditional | unverified | ACC-34 and GPU portion of ACC-04/18/19/25/35/38; B23 can report unverified if absent |

Missing Linux/GPU does not block B01 contract or CPU-core implementation. It blocks only the corresponding evidence/claim and must never be converted to `pass` by simulator/documentation.

## CI, registry and repository access

| Item | Inventory | Label |
|---|---|---|
| Git remote/revision | `origin` points to `https://github.com/ckiet1612/PBL4-Project.git`; local `main` HEAD was `9d361b8b9b919efe0427beac1027e66439a449e5` before B02 edits | observed |
| GitHub authentication/branch protection | No credential/rule inspection performed | unverified |
| GitHub Actions workflow | Read-only B02 workflow exists; Ruby/Psych command audit and actionlint 1.7.12 pass | observed |
| GitHub-hosted execution access | Credentials, branch protection and an actual hosted run were not tested | unverified |
| GHCR push/pull/signing/provenance access | Not tested; no credential read | unverified |
| Release permission | Not granted to B01; release belongs to B25 | user-provided scope |

B02 queried only public dependency/tool release registries and did not inspect GitHub credentials. Workflow behavior is locally validated; a GitHub-hosted run remains unverified and B25 separately needs explicit publish authority.

## Personnel and ownership

No named maintainer, benchmark operator, Linux host owner, security reviewer or release operator was provided. This is `unverified`, not evidence of absence. Before B20–B25, project coordination must assign people for long-running load/soak, two-host portability/relocation, GPU conditional testing, security review and release publication. No personal identity is invented in ADR/evidence.

## Inventory follow-up gates

| Before task | Required confirmation |
|---|---|
| B03/B05/B09 | B02 direct prerequisites are complete; apply each task's own simulator/PostgreSQL/Linux-Docker prerequisites before execution |
| B09/B15 | Linux/cgroups v2 Docker host and storage behavior for executor/deadline/recovery tests |
| B21 | Two independent Linux configurations, backup destination/filesystem semantics, supported image architectures |
| B22 | Reference load host capacity, exclusive benchmark window, time sync and operator for ≥3 runs/8 h soak |
| B23 | Real NVIDIA availability/driver/Container Toolkit; otherwise preserve “GPU simulated/unverified” wording |
| B25 | GHCR/GitHub release/signing permissions and clean-host operator |

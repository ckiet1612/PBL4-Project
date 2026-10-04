# Environment inventory

Inventory refreshed: **2026-09-21T13:17:19+07:00**. Labels: `observed` means a command was run on the current machine; `user-provided` means stated in approved PLAN/task/design but not observed; `unverified` means no direct evidence; `blocked` means a named missing prerequisite prevents the stated check. Inventory labels are not acceptance statuses.

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
| Docker CLI | 29.5.3 build d1c06ef | observed | CLI present; Docker Desktop VM evidence is recorded below |
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

## B09 Docker Desktop development evidence

| Item | Value | Label | Source |
|---|---|---|---|
| Docker context | `desktop-linux` | observed | `docker context show` during B09 |
| Docker Desktop | 4.79.0 | observed | `docker version` / Desktop runtime metadata |
| Engine | 29.5.3 | observed | `docker version` |
| Guest kernel | LinuxKit 6.12.76-linuxkit | observed | `docker info` |
| Guest architecture | `aarch64` / image `linux/arm64` | observed | `docker info`, image inspect |
| Guest cgroups/seccomp | cgroups v2, builtin seccomp | observed | `docker info` |
| B09 image | `nexa/cpu-iterative@sha256:341d943487940cb67f9e5ef61c2334593d8eab99619fc0f977e4f28a6d0e8e9a` | observed | image inspect and raw B09 evidence |
| B15 images | CPU `nexa/cpu-iterative@sha256:823af64e3b1389b282a92ebe0f0ae0abff7b202214d5b6d024262d8df255ec89`, worker `nexa/b15-worker:local` image ID `sha256:acf9013035cb3b47bd5c1e732aca632848d62bb5ade719713a3261423c7ce4bf` on engine 29.8.0 | observed, local only (not pushed) | `docs/evidence/raw/B15-images.json` |
| B09 boundary | Docker Desktop Linux VM, not bare Linux deployment host | observed limitation | B09 evidence report |

The VM evidence covers the production executor/entrypoint, distinct runner/workload UIDs, private control relay, deterministic result handshake, container isolation, CPU throttling, PID/scratch/RAM/log bounds, controller disconnect and authority deadline under partial IPC. PID 1 runs as `1000:1000` with all capabilities dropped and none added; the worker starts the fixed supervisor command as `1001:1000` by exact container ID, and the runner validates its one-shot registration peer. This does not prove bare Linux deployment behavior, reboot/reconciliation, two-host portability, GPU, or release acceptance.

## B16 environments (đã triển khai, chờ Task Review)

| Item | Value | Label | Source |
|---|---|---|---|
| Mac Docker Desktop engine (P) | Engine 29.8.0, kernel 7.0.12-linuxkit, `aarch64`, 8 CPU, about 3.8 GiB VM RAM | observed | `docker version` / `docker info` during B16 |
| Environment L | VPS1 (AWS EC2 c7i.2xlarge, Ubuntu 24.04.5 LTS): 8 vCPU, about 15 GiB RAM, kernel 7.0.0-1013-aws, `x86_64`/`linux/amd64`, Docker Engine 29.8.1, cgroups v2 (systemd driver), builtin seccomp, AppArmor default profile | observed | `docker info`, `uname`, `/etc/os-release` on VPS1 |
| VPS1 service exposure | PostgreSQL 17 test container and the test API listen only on 127.0.0.1; the worker runs with host networking to reach that loopback API; nothing is published publicly | observed | B16 harness and VPS1 scripts |
| B16 images (VPS1, `linux/amd64`, local only, not pushed) | `nexa/pytorch-cifar10:b16` `sha256:bd06f8a7b433264c68ed1ea0a2e8b6a6d29ac6cb499310c9e603fa04b016f9bb`, `nexa/batch-inference:b16` `sha256:2f6d0d433992a652b3d7c0134a689f5aa4b4a7afbc4cc9705662f77ed0f564f8` (base `python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9`) | observed | `tests/fixtures/workloads/*/fixture.json`, B16 evidence |
| B16 regression images (VPS1, `linux/amd64`, local only) | CPU `nexa/cpu-iterative@sha256:20fd58cb53885085338454394653d67d78f0a9ab16a1984898eefd2fa3d8d89e`, worker image id `sha256:ee004e09bc24769ea990cc2705513af225fafe579639f50b36a5aac7670ce1e7` | observed | `docker image inspect` on VPS1, B16 evidence |
| B16 regression images (Mac, `linux/arm64`, local only) | CPU `nexa/cpu-iterative@sha256:519a86971ea9d836c1c85fb8bda259302b9c74b342241c0bc6afad85d8757d45`, worker `nexa/b16-worker:local` image id `sha256:5eb661af7fdb7223437cb23958deb668673fa72bf2a1f1d0250fe3840ed54834`; both carry the final `src/nexa` (149 `.py` files, listing hash `0f684a4d…c610`). No arm64 PyTorch image was built | observed | `docker image inspect` on the Mac, B16 evidence |
| B16 round 2 images (VPS1, `linux/amd64`, local only, not pushed) | `nexa/pytorch-cifar10:b16r2` `sha256:fcad40287b78e8a7f3208168c739aaadc0c2e2004290876950c1f2ae4391e797`, `nexa/batch-inference:b16r2` `sha256:db50610aa8c93896202638ca7e731b6a0595ead3b6ea62ca331c313c4f42c4ae`, CPU `nexa/cpu-iterative@sha256:6d6d0635188a9ce88fccc09d2020ec7635dae30fa2b30f05d72ed4a0f8a549d5`, worker image id `sha256:5defa94127454e9e1a0afc74613d05d75e88729a39ef92d550d1a17a3f7abfe5` (same base) | observed | `image.history` in the fixtures, B16 evidence §Vòng 2 |
| B16 round 2 images (Mac, `linux/arm64`, local only) | CPU `nexa/cpu-iterative@sha256:95f89f118eb808ced3e475929378c40997ad0b47d6ffe27c3c9b3d0f2e194b6b`, worker `nexa/b16-worker:r2-local` image id `sha256:788a8fc820fb239adb3a559ebab38fb36981439dc18bb2503b90035843b41625`; all six round 2 images carry `src/nexa` listing hash `2486dcca…fa39` (149 files) | observed | `docker image inspect`, B16 evidence §Vòng 2 |
| VPS1 host users | UIDs 1000 and 1001 have host passwd entries (`ubuntu`, `nexa`), so `docker top` prints those names for the container runner/workload UIDs (B16-R29) | observed | `getent passwd` on VPS1 |
| B16 boundary | VPS1 is a real Linux VM (environment L). It is not bare metal, a GPU host (G), a two-host portability pair or release (R) evidence | observed limitation | B16 evidence |

VPS1 details that identify the account, network address or keys are kept out of the
repository on purpose.

## B17 environments (đã triển khai, chờ Task Review)

| Item | Value | Label | Source |
|---|---|---|---|
| Mac Docker Desktop engine (P) | Engine 29.8.1, `aarch64`, 8 CPU, `MemTotal` 4106604544 bytes (about 3.82 GiB VM RAM) | observed | `docker version` / `docker info` during B17 |
| Web toolchain | Node.js v24.21.0, pnpm 11.9.0 (Node/pnpm under `/tmp`, not on the system `PATH`) | observed | `node --version`, `pnpm --version` |
| Browser test toolchain | `@playwright/test` 1.63.0, Chromium 153.0.8010.12 (`chromium-1243`, `chromium_headless_shell-1243`) in a `PLAYWRIGHT_BROWSERS_PATH` under `/tmp`; no `install-deps` | observed | `pnpm exec playwright --version`, browser directory listing |
| B17 edge proxy image | `caddy:2.10.2-alpine` `sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d` (`linux/arm64`, pulled, not pushed) | observed | `docker image inspect` |
| B17 W2 images (Mac, `linux/arm64`, local only, not pushed) | CPU `nexa/cpu-iterative@sha256:a14b604aa9b2c8ccbe2f04b8263568fd63247cb73c069dfc4f9ce5ae18dfe934`, worker `nexa/b17-worker:local` image id `sha256:9121dc5849805e39d5f941d2c70e6460386d19a1268792a1ca75c1ea52662e37` (from `deploy/b10/Dockerfile`); both on base `python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9` | observed | `docker image inspect`, B17 evidence |
| B17 service exposure | Caddy TLS (internal CA) on 127.0.0.1 is the only browser origin; the test API binds 127.0.0.1 and `/v1/internal/*` answers 404 at Caddy; PostgreSQL 17 test container on 127.0.0.1:15439 | observed | `scripts/b17_e2e_stack.py`, `deploy/web/Caddyfile` |
| B17 boundary | Playwright runs against the Docker Desktop VM (environment P). It is not Linux host (L), GPU (G) or release (R) evidence; no VPS1 run was made for B17 | observed limitation | B17 evidence |

## B19 environments (đã triển khai, chờ Task Review)

| Item | Value | Label | Source |
|---|---|---|---|
| Mac Docker Desktop engine (P) | Engine 29.8.1, `aarch64` (same VM as B17) | observed | `docker version` during B19 |
| Python metrics library | `prometheus-client` 0.26.0 (the only dependency B19 adds) | observed | `uv.lock` |
| Prometheus / promtool | `prom/prometheus` v3.5.0 `sha256:63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996` (pulled by digest, `linux/arm64`, not pushed); used only for `promtool` and the test scrape | observed | `docs/evidence/raw/B19-promtool.out`, `deploy/prometheus/` |
| B19 images (Mac, `linux/arm64`, local only, not pushed) | CPU `nexa/cpu-iterative@sha256:a35ca28733855ab40ff10207f69d94c028338073b74984ef9d0b9f3783b0d956` (rebuilt because the build context `src/nexa` changed; the 18-file runner import closure is byte-identical to the B18 image `ec419c61…`, see `docs/evidence/raw/B19-cpu-image-runner-closure.sha256`), worker `nexa/b19-worker:local` image id `sha256:d563c2c33257b5e2c6612eb2a06b89be7d337be3c6ee6105e49b8f866ba2f8cf` (from `deploy/b10/Dockerfile`); base `python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9` | observed | `docker image inspect`, `docs/evidence/raw/B19-build-*.out` |
| B19 boundary | Docker, scrape, outage and disk-full (tmpfs ENOSPC) runs are on the Docker Desktop VM (P). Not Linux host (L), GPU (G) or release (R) evidence; no VPS1 run | observed limitation | B19 evidence |

## B02 prerequisite closure

`uv.lock`, frozen Python install/quality checks, Node 24 LTS web checks and CI syntax/semantics are verified. No direct B02 blocker remains. Missing `psql`, bare Linux deployment, PostgreSQL runtime, two-host and GPU evidence belong to later tasks and are not B02 blockers; B09's Docker Desktop VM evidence is recorded separately and does not close those gates.

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
| GitHub-hosted execution access | Hosted runs execute on push: B11 run `35964010775` was the last green `Python quality`; B12 through the B01–B16 remediation (latest `36696888507`, cancelled at the 15-minute job timeout) are red from the job timeout, Typer ANSI styling under `GITHUB_ACTIONS` and runner pg_dump 16 against the PostgreSQL 17 service. CI-python-fix addresses these; the post-fix hosted run is pending | observed (post-fix run pending) |
| GHCR push/pull/signing/provenance access | Not tested; no credential read | unverified |
| Release permission | Not granted to B01; release belongs to B25 | user-provided scope |

B02 queried only public dependency/tool release registries and did not inspect GitHub credentials. GitHub-hosted runs have executed (see the row above; evidence in `docs/evidence/CI-python-fix.md`); whether the fixed workflow is green awaits a new hosted run. GitHub credentials and branch protection remain unverified, and B25 separately needs explicit publish authority.

## Personnel and ownership

No named maintainer, benchmark operator, Linux host owner, security reviewer or release operator was provided. This is `unverified`, not evidence of absence. Before B20–B25, project coordination must assign people for long-running load/soak, two-host portability/relocation, GPU conditional testing, security review and release publication. No personal identity is invented in ADR/evidence.

## Inventory follow-up gates

| Before task | Required confirmation |
|---|---|
| B03/B05/B09 | B02 direct prerequisites are complete; B09 implementation and direct Docker Desktop Linux VM scenarios are observed; retain the VM boundary in review evidence |
| B09/B15 | Bare Linux/cgroups v2 Docker host and storage behavior before bare-deployment/reboot/recovery acceptance claims |
| B21 | Two independent Linux configurations, backup destination/filesystem semantics, supported image architectures |
| B22 | Reference load host capacity, exclusive benchmark window, time sync and operator for ≥3 runs/8 h soak |
| B23 | Real NVIDIA availability/driver/Container Toolkit; otherwise preserve “GPU simulated/unverified” wording |
| B25 | GHCR/GitHub release/signing permissions and clean-host operator |

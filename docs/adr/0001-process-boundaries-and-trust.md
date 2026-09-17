# ADR-0001: Process boundaries and trust zones

- **Date:** 2026-09-17
- **Status:** accepted
- **Decision source:** PLAN §1–§3, §8–§10; this ADR restates approved boundaries. Wire naming is B01 concretization authorized by the B01 task.

## Context

Nexa must expose API, CLI and Web UI for multiple tenants on one Linux server while only one trusted component accesses Docker. Authorization/state must remain backend-owned, and worker/workload credentials must not collapse into one trust domain.

## Options considered

1. Approved modular monolith: separate API and coordinator processes, local worker through REST, trusted runner/workload containers, PostgreSQL/filesystem authority.
2. API directly controls Docker. Simpler initially but violates the worker-only socket boundary and enlarges API compromise impact.
3. Independent services/message broker or multi-node enrollment. Adds distributed failure modes and enters Future Work.

## Decision

Use option 1. Caddy/TLS exposes UI/API. UI and CLI use the same `/v1` contract. API owns authentication, authorization, validation and user/admin operations. Coordinator owns leader-gated scheduling but does not call Docker. Local worker is the sole Docker client and uses worker-scoped REST credentials. Trusted runner enforces deadlines/isolation; workload is untrusted relative to worker/runner and has no credential, Docker socket, network or arbitrary invocation.

Bootstrap uses an authenticated maintenance-network secret. One operation creates the first user plus `SYSTEM_ADMIN` grant only while no enabled system administrator exists, then permanently closes that bootstrap path; another configures/rotates only the single local worker identity in an explicit window. Neither is public enrollment. Browser session, CLI token, worker credential and bootstrap secret are distinct principals.

Every public tenant-scoped operation requires explicit `X-Nexa-Tenant-Id`; authorization, cursor binding, audit and idempotency use that context. Tenant membership roles are `MEMBER`/`TENANT_ADMIN`. `SYSTEM_ADMIN` is a separate global grant, never an implicit membership. Cross-tenant reads use audited `/admin` operations; the global role does not impersonate a tenant principal for artifacts, submit or job control.

## Consequences

- Cross-process contracts must be explicit and versioned; REST loss/replay is normal.
- Worker is a privileged trusted component requiring hardening/audit; workload compromise cannot become control-plane authority.
- Coordinator turnover cannot invalidate healthy attempt authority.
- Multi-server/shared-store/Kubernetes designs are excluded from v1.

## Transition and rollback

B02–B10 implement process entrypoints/composition within approved module placement. Any proposal to give API/coordinator Docker access or add remote worker enrollment changes PLAN first. There is no data migration in B01.

## Acceptance

ACC-01, ACC-02, ACC-12, ACC-24–27, ACC-32 and ACC-39. Evidence: dependency/import tests, network/container inspection, auth matrix tests and clean deployment; ADR alone is not runtime evidence.

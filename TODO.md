# TODO — Homelab Repository Findings

Findings from a full repo review, written as self-contained tasks for future sessions.
Sorted by **priority × impact** (highest first). Each item notes where to look, why it
matters, and a suggested fix.

Legend — Priority: `P0` (do first) → `P3` (nice to have). Impact: High / Medium / Low.

---

## P0 — Correctness, data loss & security

### 1. Loki retention is configured via the deprecated `table_manager` and likely never deletes logs ✅ DONE
- **Status:** Fixed — replaced `table_manager` with a `compactor` (`retention_enabled: true`,
  `delete_request_store: filesystem`) and moved retention to `limits_config.retention_period: 720h`.
- **Priority:** P0 · **Impact:** High (unbounded disk growth → can fill the DockerVM disk and crash the stack)
- **Where:** `docker/stacks/observability/loki.yaml` (lines 32-34)
- **Problem:** Retention is set with `table_manager.retention_period: 30d`. With the `tsdb` +
  `filesystem` store used here, `table_manager` does **not** perform deletes — Loki requires the
  **compactor** with `retention_enabled: true` to actually delete old chunks. As written, logs
  grow forever.
- **Fix:**
  - Remove the `table_manager` block.
  - Add a `compactor` section with `retention_enabled: true`, `delete_request_store: filesystem`,
    a `working_directory`, and a `compaction_interval`.
  - Move retention into `limits_config` (`retention_period: 720h`) and optionally per-stream
    overrides.
  - Verify after deploy that old chunks under `loki-data` are pruned.

### 2. `task ansible:docker:restart STACK=...` enum is wrong — `timescaledb` cannot be restarted ✅ DONE
- **Status:** Fixed — enum changed to `[observability, roastbeef-swag, timescaledb]` in
  `ansible/taskfiles/docker.yaml` and the matching list updated in `CLAUDE.md`. (Asserting that
  `target_stack` matches a real stack is tracked under item 3.)
- **Priority:** P0 · **Impact:** High (documented command silently does nothing / rejects valid input)
- **Where:** `ansible/taskfiles/docker.yaml` (line 20 enum), echoed in `CLAUDE.md`
- **Problem:** The enum is `[observability, roastbeef-swag, ha-timescaledb]`, but the actual stack
  directory is `timescaledb` (see `docker/stacks/`). So:
  - `STACK=timescaledb` is **rejected** by the enum.
  - `STACK=ha-timescaledb` passes the enum but `target_stack` never matches a directory, so the
    `when` filter in `deploy_docker_stacks` skips every stack — **no error, nothing happens**.
- **Fix:** Change the enum to `[observability, roastbeef-swag, timescaledb]` and update the
  matching list in `CLAUDE.md`. Consider failing the play if `target_stack` matches no directory
  (see item 3).

### 3. `deploy_docker_stacks` uses `ignore_errors: true` — failed deploys report success ✅ DONE
- **Status:** Fixed — replaced blanket `ignore_errors` with `failed_when: false` on the per-stack
  loop (so all stacks are still attempted) plus a final `fail` task that aborts the play and lists
  any stacks whose `docker compose` returned non-zero. Also added an `assert` that a non-empty
  `target_stack` matched at least one discovered stack (the safety net deferred from item 2).
- **Priority:** P0 · **Impact:** High (a stack that fails to come up looks like a successful deploy)
- **Where:** `ansible/roles/deploy_docker_stacks/tasks/main.yaml` (line 24)
- **Problem:** `ignore_errors: true` on the `docker compose` loop swallows all failures. A broken
  `compose.yaml`, image pull failure, or port conflict produces a green run. Combined with item 2,
  a typo'd `target_stack` is also indistinguishable from success.
- **Fix:** Drop blanket `ignore_errors`. Register the loop result and use a
  `failed_when`/`block`+`rescue` so one bad stack is reported (and ideally doesn't abort the
  others, but is surfaced at the end). Optionally assert that `target_stack`, when non-empty,
  matched at least one discovered compose file.

### 4. Grafana exposed with no admin password + anonymous access + embedding enabled ✅ DONE
- **Status:** Fixed the takeover risk — added `GF_SECURITY_ADMIN_PASSWORD` (sourced from
  `docker/stacks/observability/.env`, with a `:?` guard so deploy fails loudly if it's unset) plus a
  tracked `.env.example`. Anonymous Viewer and embedding are intentionally kept (embedding is used by
  Home Assistant; Homer links to Grafana directly over LAN), and the `3000` bind is left as-is for
  the same reason. **Manual step:** set a strong `GF_SECURITY_ADMIN_PASSWORD` in the VM's `.env`
  before the next deploy.
- **Priority:** P0 · **Impact:** High (security)
- **Where:** `docker/stacks/observability/compose.yaml` (grafana service, lines 2-15)
- **Problem:** No `GF_SECURITY_ADMIN_PASSWORD` is set, so the admin account is the default
  `admin/admin`. `GF_AUTH_ANONYMOUS_ENABLED=true` and `GF_SECURITY_ALLOW_EMBEDDING=true` are on,
  and the port is bound to `0.0.0.0:3000`. Anyone on the LAN can reach it; default creds = full
  takeover.
- **Fix:** Set `GF_SECURITY_ADMIN_PASSWORD` from an env/secret (vault or `.env`). Keep anonymous
  Viewer if desired, but ensure the admin password is strong. Consider restricting the port bind
  if Grafana is only consumed via Tailscale/reverse proxy.

### 5. Database and metrics ports bound to all interfaces (LAN-exposed)
- **Priority:** P0 · **Impact:** High (security)
- **Where:** `docker/stacks/timescaledb/compose.yaml` (`5432:5432`),
  `docker/stacks/observability/compose.yaml` (`9090:9090` with `--web.enable-remote-write-receiver`)
- **Problem:** TimescaleDB/Postgres is exposed on `0.0.0.0:5432` to the whole LAN with a single
  shared `admin` user. Prometheus is exposed on `9090` **with the remote-write receiver enabled**,
  so any LAN host can both read and inject metrics. Neither has auth in front of it.
- **Fix:** Bind sensitive ports to localhost or the Tailscale interface (e.g.
  `127.0.0.1:5432:5432`) and/or front them with the reverse proxy + auth. Restrict who can reach
  the Prometheus write endpoint. At minimum, document the exposure decision.

---

## P1 — Reliability & maintainability

### 6. Commit the pending Prometheus out-of-order config change
- **Priority:** P1 · **Impact:** Medium (uncommitted working state; the prior config likely broke startup)
- **Where:** working tree diff on `docker/stacks/observability/compose.yaml` + `prometheus.yaml`
- **Problem:** The change removes `--storage.tsdb.out-of-order-time-window=3h` from the Prometheus
  command line and moves it into `prometheus.yaml` under `storage.tsdb.out_of_order_time_window`.
  The CLI form is **not a valid Prometheus flag** (out-of-order ingestion is config-only), so the
  previous version would have failed to start. This fix is correct but currently uncommitted.
- **Fix:** Verify Prometheus starts cleanly with the new config, then commit the change with a
  message explaining the flag→config migration.

### 7. Docker images all pin to `:latest`
- **Priority:** P1 · **Impact:** Medium (non-reproducible deploys; surprise breaking upgrades on every `up`)
- **Where:** `docker/stacks/observability/compose.yaml` (grafana, prometheus, loki, alloy,
  node-exporter, cadvisor), `docker/stacks/roastbeef-swag/compose.yaml`
- **Problem:** Every `docker compose up` may pull a new major version. Loki/Prometheus/Grafana have
  had breaking config changes between versions; an unattended `proxmox:update` could pull one in.
- **Fix:** Pin to explicit tags (e.g. `grafana/loki:3.x`, `prom/prometheus:v3.x`). Use Renovate/
  Dependabot or a periodic manual bump to control upgrades. (Dockge already pins `:1`,
  TimescaleDB pins `pg18` — follow that pattern.)

### 8. `ansible.cfg` has a typo: `std_callback` should be `stdout_callback`
- **Priority:** P1 · **Impact:** Medium (intended YAML output formatting is silently not applied)
- **Where:** `ansible/ansible.cfg` (line 5)
- **Problem:** `std_callback = yaml` is not a valid `[defaults]` key, so it is ignored and playbook
  output uses the default callback instead of the YAML one.
- **Fix:** Rename to `stdout_callback = yaml`. Confirm the `ansible.posix`/`community.general`
  collection providing the YAML callback is installed (it is, via requirements).

### 9. The Proxmox host (`pve`) and PBS LXC are not in inventory — `proxmox:update` never updates them
- **Priority:** P1 · **Impact:** Medium (the actual hypervisor + backup server miss automated patching)
- **Where:** `ansible/inventory/hosts.ini`, `ansible/playbooks/proxmox.yaml`
- **Problem:** Inventory only contains `vm-docker` (192.168.0.69) and `lxc-tailscale`
  (192.168.0.155). The PVE host (192.168.0.100) and PBS LXC (192.168.0.101, per
  `proxmox-backup-server-setup.md`) are absent, so `task ansible:proxmox:update` only patches the
  two guests — the name implies it patches Proxmox itself.
- **Fix:** Either add `pve` and the PBS LXC to inventory with an appropriate group and update the
  playbook, or rename the task/docs to reflect that it patches guests only. Be deliberate about
  rebooting the hypervisor (snapshot/ordering implications).

---

## P2 — Cleanup & documentation

### 10. `retention_days: 21` is defined but never used; snapshot retention is hardcoded to 2
- **Priority:** P2 · **Impact:** Low (dead config / inconsistency vs. docs)
- **Where:** `ansible/inventory/group_vars/all/vars.yaml` (line 8),
  `ansible/roles/snapshot_proxmox_machines/tasks/main.yaml` (line 11, `retention: 2`)
- **Problem:** `retention_days` is referenced nowhere in the codebase, yet `CLAUDE.md` lists it as a
  meaningful inventory var. The snapshot role hardcodes `retention: 2`.
- **Fix:** Either wire `retention_days` (or a dedicated `snapshot_retention`) into the snapshot role
  and remove the magic number, or delete the unused var and correct `CLAUDE.md`.

### 11. TimescaleDB requires a manual `.env` (`POSTGRES_PASSWORD`) with no documentation or example
- **Priority:** P2 · **Impact:** Medium (undocumented manual step; fresh deploy fails or starts with empty password)
- **Where:** `docker/stacks/timescaledb/compose.yaml` (line 10), `.gitignore` (`**/.env`)
- **Problem:** `POSTGRES_PASSWORD=${POSTGRES_PASSWORD}` reads from a `.env` that is gitignored and
  never created by Ansible. Since `.env` doesn't exist locally and rsync push has no `--delete`, the
  file must be hand-placed on the VM — undocumented. Secret management is also inconsistent: PVE/
  snapshot secrets live in Ansible Vault, but this one lives in a loose `.env`.
- **Fix:** Add a `timescaledb/.env.example`, document the step in README/CLAUDE.md, or template the
  `.env` from Vault during deploy so secret handling is consistent.

### 12. `sync_docker_stacks` rsync has no `--delete`; deletions never propagate (and pull re-adds them)
- **Priority:** P2 · **Impact:** Medium (removing a stack locally leaves it running on the VM)
- **Where:** `ansible/roles/sync_docker_stacks/tasks/main.yaml`
- **Problem:** The push (local→VM) has no `--delete`, so a stack deleted from the repo stays on the
  VM. The subsequent pull (VM→local, excluding `dockge`) would even copy the "deleted" stack back
  into the repo. Net effect: stacks can only be added, never removed, via this flow.
- **Fix:** Decide the intended model. If the repo is the source of truth, add controlled `--delete`
  on push (carefully, with the existing `data` exclusion). If the bidirectional sync is intentional,
  document the deletion procedure.

### 13. Alloy: Dockge container logs are rejected (UTC timezone) — known TODO
- **Priority:** P2 · **Impact:** Low/Medium (a service's logs are missing from Loki)
- **Where:** `docker/stacks/observability/main.alloy` (line 60 TODO)
- **Problem:** Dockge logs are rejected, suspected to be because the container runs in UTC, tripping
  Loki's `reject_old_samples` / timestamp handling.
- **Fix:** Investigate timestamp parsing in the `loki.source.docker` pipeline (add a
  `stage.timestamp`/relabel to normalize), or align the container timezone. Confirm Dockge logs
  appear in Loki afterward.

### 14. Prometheus has no explicit retention/size limit
- **Priority:** P2 · **Impact:** Low/Medium (relies on the 15d default; disk usage not bounded by size)
- **Where:** `docker/stacks/observability/compose.yaml` (prometheus command, lines 26-29)
- **Problem:** No `--storage.tsdb.retention.time` or `--storage.tsdb.retention.size` is set, so the
  default 15d applies and there's no size cap. Worth making explicit alongside the Loki retention
  work (item 1).
- **Fix:** Set explicit `--storage.tsdb.retention.time` and/or `--storage.tsdb.retention.size` to
  match the disk budget.

### 15. README is incomplete / drifting from actual setup
- **Priority:** P2 · **Impact:** Low
- **Where:** `README.md`
- **Problem:** README omits the `timescaledb` stack, the `ansible:docker:restart` command, and the
  Proxmox Backup Server (documented separately in `proxmox-backup-server-setup.md`). Homer lists
  hosts (TrueNAS at .139, many apps) that aren't reflected in the architecture docs.
- **Fix:** Refresh README to list all stacks and tasks, link the PBS setup doc, and reconcile the
  host/IP inventory between `CLAUDE.md`, `homer/config.yml`, and the Ansible inventory.

---

## P3 — Minor / nice-to-have

### 16. roastbeef-swag data volume write-permission TODO
- **Priority:** P3 · **Impact:** Low
- **Where:** `docker/stacks/roastbeef-swag/compose.yaml` (line 9 TODO)
- **Problem:** Existing TODO: "Fix write permissions on VM" for the `./data` bind mount.
- **Fix:** Set a matching `user:`/ownership for the data dir, or switch to a named volume.

### 17. `host_key_checking = False` in `ansible.cfg`
- **Priority:** P3 · **Impact:** Low (acceptable for a LAN homelab, but disables host-key verification)
- **Where:** `ansible/ansible.cfg` (line 4)
- **Fix:** Optionally pre-populate `known_hosts` and re-enable host key checking for a small
  security/MITM hardening win.

### 18. Homer dashboard has no automated deploy (existing TODO)
- **Priority:** P3 · **Impact:** Low
- **Where:** `homer/config.yml` (line 61 TODO)
- **Problem:** Existing "nice to have" TODO to add an Ansible playbook to deploy/update the Homer
  dashboard. There's currently no stack or automation for Homer in the repo.
- **Fix:** Add a Homer compose stack and/or a sync task so the dashboard config is deployed like the
  other stacks.

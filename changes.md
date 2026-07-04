# Changes — Repository Review Remediation

Summary of the changes made while working through `TODO.md`. All work landed on the
`code-review` branch. Items are grouped by what changed; **⚠️ MANUAL STEP** marks
actions you must take on the live infrastructure for a change to take effect safely.

---

## ⚠️ Open manual steps (do these before/at the next deploy)

1. **Set `GF_SECURITY_ADMIN_PASSWORD`** in `docker/stacks/observability/.env` on the
   DockerVM. Grafana now **refuses to start** without it (a `:?` guard replaces the old
   default `admin/admin`). See `.env.example`.
2. **Set `POSTGRES_PASSWORD`** in `docker/stacks/timescaledb/.env` on the DockerVM.
   TimescaleDB now **refuses to start** without it (same `:?` guard). See `.env.example`.
3. **Add the real PBS `vmid`** to `ansible/inventory/hosts.ini` (`lxc-pbs` line) so PBS
   gets pre-update snapshots. Until then it is **patched but not snapshotted**
   (`pct list` on the PVE host gives the id).
4. **Be aware `proxmox:update` now patches and reboots the PVE hypervisor (last).**
   Rebooting the host takes down every guest. Review before running; consider gracefully
   stopping guests first.
5. **Install the Renovate GitHub App** on `maribowman/homelab` so the committed
   `renovate.json` takes effect: https://github.com/apps/renovate → Configure → grant the
   repo access. The first run creates a **Dependency Dashboard** issue and opens update PRs.

---

## Observability stack

### Loki retention (was broken)
- `docker/stacks/observability/loki.yaml`: replaced the deprecated `table_manager`
  (which does **not** delete with the tsdb+filesystem store) with a `compactor`
  (`retention_enabled: true`, `delete_request_store: filesystem`) and moved retention to
  `limits_config.retention_period: 720h` (30d). Logs are now actually pruned.

### Prometheus
- Moved out-of-order ingestion from an **invalid** CLI flag
  (`--storage.tsdb.out-of-order-time-window`, which would have failed startup) to
  `prometheus.yaml` → `storage.tsdb.out_of_order_time_window: 3h`. Validated with
  `promtool check config` (SUCCESS).
- Set explicit TSDB retention: `--storage.tsdb.retention.time=30d` (was the implicit 15d
  default), aligned with Loki. Optional size cap noted inline for later.

### Grafana
- Added `GF_SECURITY_ADMIN_PASSWORD` sourced from `.env` with a `:?` guard (see manual
  step 1). Anonymous Viewer + embedding + the `:3000` LAN bind kept on purpose (HA
  embedding / Homer link). Added `docker/stacks/observability/.env.example`.

### Image pinning (was all `:latest`)
- `prometheus:v3`, `loki:3`, `node-exporter:v1` (floating major tags where published),
  `grafana-oss:13.0.2`, `alloy:v1.16.2`, `cadvisor:v0.55.1` (concrete where no major tag
  exists). Prevents surprise major upgrades. `roastbeef-swag` left on `:latest` (own CD'd
  app, no enumerable tags).

### Documented LAN exposure (accepted)
- Inline comments at `timescaledb:5432` and `prometheus:9090` documenting the deliberate
  all-interfaces bind (HA + remote-write need it on the trusted LAN). No bind change.

## TimescaleDB
- Added `docker/stacks/timescaledb/.env.example` and a `:?` guard on `POSTGRES_PASSWORD`
  (see manual step 2). Documented the requirement in `CLAUDE.md`.

## Ansible

### Taskfile restart enum (bug)
- `ansible/taskfiles/docker.yaml`: enum `ha-timescaledb` → `timescaledb` (the real stack
  dir). The valid name was previously rejected and the wrong one silently no-op'd.

### Deploy role error handling (bug)
- `ansible/roles/deploy_docker_stacks/tasks/main.yaml`: removed blanket
  `ignore_errors: true` that turned failed deploys green. Now attempts every stack
  (`failed_when: false`) and fails the play at the end listing any that returned non-zero.
  Also asserts a non-empty `target_stack` matches a real stack.

### ansible.cfg YAML output (bug)
- `ansible/ansible.cfg`: the invalid `std_callback = yaml` was silently ignored. The
  obvious rename `stdout_callback = yaml` would **also** fail (the `yaml` callback was
  removed from `community.general` 12.0.0). Set `callback_result_format = yaml` instead.
  Verified YAML rendering.

### Proxmox host + PBS in inventory
- `ansible/inventory/hosts.ini`: added `pve` (new `[pve_hosts]` group) and `lxc-pbs`
  (`[debian_nodes]`).
- `ansible/roles/snapshot_proxmox_machines/tasks/main.yaml`: snapshot only
  `when: vmid is defined` (the PVE host can't snapshot itself).
- `ansible/playbooks/proxmox.yaml`: split into two plays so guests are
  snapshotted/patched/restored first and the **PVE host is patched + rebooted last**
  (see manual step 4).

### Snapshot retention var
- Replaced the unused `retention_days: 21` with `snapshot_retention: 2`, wired into the
  snapshot role (was hardcoded `2`). Updated `CLAUDE.md`.

## Dependency automation

### Renovate (image update PRs) — #7 follow-up
- Added `renovate.json` at the repo root (`config:recommended`, PR-only — no automerge).
  Renovate's docker-compose manager tracks the now-pinned image tags across
  `docker/stacks/*/compose.yaml` and `docker/dockge/compose.yaml`, opening a review PR when a
  newer version is available (weekly Monday schedule, `prConcurrentLimit: 5`).
- Observability-stack images are grouped into one PR; `timescale/timescaledb-ha` gets a custom
  `regex` versioning rule so its `pgNN` tags are parsed. The two `:latest` images (`logporter`,
  `roastbeef-swag`) are intentionally left untracked (no digest pinning).
- Requires the Renovate GitHub App (see manual step 5). Validate locally with
  `npx --yes --package renovate -- renovate-config-validator renovate.json`.

## Documentation
- `README.md`: added the TimescaleDB stack, the `docker:restart` command, the required
  `.env` files, a Backups section linking `proxmox-backup-server-setup.md`, and the
  updated `proxmox:update` behavior.
- `CLAUDE.md`: corrected the restart command list, the `proxmox:update` description, the
  inventory-vars list, and the TimescaleDB `.env` requirement.
- `TODO.md`: each item annotated with status (✅ done / ⏸️ deferred-accepted) plus a
  Progress summary.

---

## Deferred / accepted (no code change)

| # | Item | Why |
|---|------|-----|
| 12 | rsync `--delete` | Needs a sync-model decision (repo-as-source-of-truth vs bidirectional); `--delete` risks data loss. |
| 13 | Alloy Dockge logs rejected | Needs live `alloy`/`loki` logs to confirm the cause; refined into diagnostic steps in TODO. |
| 16 | roastbeef `./data` perms | Needs the image's runtime UID / VM dir ownership; on-VM options documented in TODO. |
| 17 | `host_key_checking = False` | Accepted LAN-homelab tradeoff; re-enabling needs `known_hosts` seeding first. |
| 18 | Homer auto-deploy | Nice-to-have; would add a new container and move config. |

## Optional follow-ups (noted inline in TODO)
- Prometheus `--storage.tsdb.retention.size` cap once the disk budget is known (#14).
- Vault-template the `.env` secrets for consistent secret handling (#4/#11).

---

## Verification done locally
- `promtool check config` on `prometheus.yaml` (SUCCESS).
- `ansible-playbook --syntax-check` on `proxmox.yaml` and `docker.yaml`.
- `ansible-inventory --graph` (confirms `pve_hosts`/`lxc-pbs`).
- `ansible-config` + `ansible -m debug` (confirms YAML output).

Not validated here (require the live VM): a real `task ansible:docker:deploy` and
`task ansible:proxmox:update`.

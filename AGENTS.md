# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Personal homelab infrastructure managed with [Ansible](https://docs.ansible.com/) and [Taskfile](https://taskfile.dev/). The main host is a Proxmox server (`pve`, 10.0.10.10) running a DockerVM (`vm-docker`, 10.0.40.40), a Tailscale LXC (`lxc-tailscale`, 10.0.40.90), a Proxmox Backup Server LXC (`lxc-pbs`, 10.0.40.20) and a control LXC (`lxc-ansible`) that runs the playbooks on systemd timers. A Synology NAS is at 10.0.40.10 and holds the PBS datastore. A TrueNAS SCALE 25.10 server (`truenas`) runs apps; Ansible upgrades them weekly.

Git is the source of truth: edits made in the Dockge UI drift and get overwritten. TrueNAS OS updates and the Home Assistant instances are out of scope.

## Common Commands

All commands use `task` (Taskfile runner). Dependencies are bootstrapped automatically — `task ansible:init` is a dep of every ansible task and only runs when `requirements.txt`/`requirements.yaml` change. Every task that runs a playbook passes extra args through after `--`, e.g. `task ansible:proxmox:update -- --limit lxc-tailscale`.

```sh
# Docker
task ansible:docker:deploy          # Rsync stacks to DockerVM, pull images, up -d --remove-orphans, health check
task ansible:docker:restart STACK=observability  # Restart a single stack (observability | roastbeef-swag | timescaledb)

# Proxmox maintenance
task ansible:proxmox:update         # Preflight -> snapshot -> update guests one by one (PBS last) -> PVE host (no reboot) -> summary
task ansible:proxmox:check          # Same, with --check --diff
task ansible:proxmox:rollback HOST=lxc-tailscale  # Roll a guest back to its newest pre_update_* snapshot (confirm, or -y)
task ansible:proxmox:syntax         # Syntax-check the proxmox and rollback playbooks
task ansible:proxmox:ping           # Test SSH connectivity to all hosts

# TrueNAS apps
task ansible:truenas:update         # Upgrade every app to its newest non-major version, pull custom-app images, report majors/OS updates -> summary
task ansible:truenas:check          # Same, with --check --diff (computes the plans, upgrades nothing)
task ansible:truenas:rollback APP=immich [VERSION=1.2.3]  # app.rollback to VERSION or the newest rollback version, with snapshot (confirm, or -y)
task ansible:truenas:prune          # Delete unused app images (keep in-use + 2 rollback images per repository) -> summary
task ansible:truenas:prune-check    # Same, with --check --diff (lists what would be deleted and freed)
task ansible:truenas:syntax

# Weekly maintenance (what the Sunday timer runs)
task ansible:maintenance:run        # Proxmox update + TrueNAS apps -> one summary
task ansible:maintenance:check
task ansible:maintenance:syntax

# Backups (PBS + PVE backup job)
task ansible:backup:apply           # Configure PBS mount/datastore/jobs/notifications and the PVE storage/vzdump job
task ansible:backup:check           # --check --diff of the above
task ansible:backup:syntax

# Control node
task ansible:control:provision      # Create the control LXC via the PVE API
task ansible:control:apply          # Configure it (run from the laptop: copies the vault, distributes SSH keys)
task ansible:control:syntax
```

## Schedule

| When | What | Driver |
|---|---|---|
| Daily 01:00 | vzdump of all guests except PBS (1001) → PBS → Synology | native PVE job (`pve_backup_jobs`) |
| Daily (PBS) | prune (3 last / 3 weekly / 3 monthly), GC, verify | native PBS jobs (`pbs_server`) |
| Daily 05:00 | `git pull`; if HEAD differs from the last deployed commit → `docker:deploy` | `homelab-deploy.timer` on `lxc-ansible` |
| Sun 04:00 | `git pull` → `maintenance:run` (Proxmox, then TrueNAS apps) | `homelab-maintenance.timer` on `lxc-ansible` |
| Continuous | Renovate PRs; patch/minor/digest automerge when CI is green | GitHub (`renovate.json`, `.github/workflows/ci.yaml`) |

Every playbook run ends in **one** Discord summary (`run_report`); its embed color is the worst result: green ok, yellow done with time-uncritical todos (e.g. PVE reboot pending, stale backup), red failed with manual todos (e.g. the rollback command). The daily deploy only posts when not green (`-e run_report_notify=not_ok`); the weekly run always posts.

Both services share `/run/homelab.lock` and have `OnFailure=homelab-notify@%n.service`, which posts the last 30 journal lines to Discord, unless the playbook already posted its summary (marker `/var/lib/homelab/report-sent.<unit>`, passed as `HOMELAB_REPORT_MARKER`). So the journal dump only shows up for failures before the summary (git pull, venv, aborted run).

## Architecture

### Ansible

- Venv lives at `ansible/.venv`; all ansible binaries must be invoked via that venv (the Taskfile handles this automatically).
- Secrets are stored in `ansible/inventory/group_vars/all/vault.yaml` (Ansible Vault). The vault password file is expected at `ansible/inventory/group_vars/all/.vault.pass` (gitignored, must be provisioned locally). Vault keys: `pve_token_secret`, `pbs_user_password` (`proxmox@pbs`), `synology_cifs_password`, `discord_webhook_url`, and `docker_stack_env` (per-stack `.env` contents, e.g. `observability: {GF_SECURITY_ADMIN_PASSWORD: …}`, `timescaledb: {POSTGRES_PASSWORD: …}`). After a vault change, run `control:apply` again so the control node gets the new copy.
- Inventory groups (`ansible/inventory/hosts.ini`): `proxmox_hosts`, `pbs_hosts`, `ubuntu_nodes`, `debian_nodes`, `docker_hosts`, `control_nodes`, `truenas_hosts`, and `guests` (= ubuntu + debian + pbs). Every host except `pve` has a `vmid`. `control_nodes` is deliberately not in `guests`; it updates itself via unattended-upgrades. `truenas` (`ansible_user=ansible`, no `vmid`) is not in `guests` either and is excluded from `ssh_access` (its keys are managed in the TrueNAS UI).
- Shared vars are in `ansible/inventory/group_vars/all/vars.yaml`: Proxmox API details, PBS/NAS addresses, backup schedule and retention, snapshot prefix/retention, timer `OnCalendar` values, control-node repo settings, `dockge_stacks_path` (`/opt/stacks` on DockerVM). Role-specific vars live in each role's `defaults/main.yaml`, prefixed with the role name.
- Collections used: `community.proxmox`, `community.general`, `ansible.posix`.
- CI (`.github/workflows/ci.yaml`) runs yamllint, ansible-lint (profile `production`), `pytest ansible/tests` (TrueNAS version and image prune logic), `--syntax-check` of every `ansible/playbooks/*.yaml` with a stub vault, and `docker compose config -q` per stack.

### Playbooks

| Playbook | What it does |
|---|---|
| `proxmox.yaml` | Preflight backup check → guests `serial: 1` (snapshot, docker down, apt, reboot, docker up + health) → PBS → PVE host (upgrade, never reboots, warns when a reboot is pending) → one Discord summary. A failed guest stops all further updates (`not_run`) and the run fails after the summary. Imports `include/proxmox_update.yaml` + `include/run_summary.yaml`. |
| `rollback.yaml` | `-e target=<host>` [`-e snapshot=<name>`]: rolls back to the newest `pre_update_*` snapshot and starts the guest. |
| `truenas.yaml` | TrueNAS apps: image prune → free-space guard → `update_truenas_apps` (only when the guard passed) → one Discord summary (`TrueNAS`). Imports `include/truenas_update.yaml` + `include/run_summary.yaml`. |
| `truenas_prune.yaml` | `prune_truenas_images` on its own → one Discord summary (`TrueNAS prune`). For manual runs; the weekly run prunes too. |
| `maintenance.yaml` | The Sunday run: `include/proxmox_update.yaml` + `include/truenas_update.yaml` → one Discord summary (`Maintenance`). TrueNAS runs even when a guest update failed. |
| `truenas_rollback.yaml` | `-e app=<name>` [`-e version=<v>`]: `app.rollback` (with `rollback_snapshot`) to the given version or the newest of `app.rollback_versions`; one Discord message. |
| `docker.yaml` | `sync_docker_stacks` (tag `sync`) then `deploy_docker_stacks` (tag `deploy`) → one Discord summary (`Docker deploy`). |
| `include/*.yaml` | Fragments for `import_playbook` (not run directly): `proxmox_update.yaml` (preflight, guests, PBS, PVE), `truenas_update.yaml` (TrueNAS image prune, free-space guard, apps) and `run_summary.yaml` (sends the run summary, fails the run when red; import it last, with `run_report_title`). |
| `backup.yaml` | `pbs_server` on PBS → `pve_backup_jobs` on PVE → `qemu_guest_agent` on Ubuntu VMs. Idempotent: reads JSON state first, changes only on a diff. |
| `control.yaml` | Play 1 (tag `provision`): create the control LXC. Play 2 (tag `apply`): `ssh_access` + `control_node`. |

### Ansible Roles

| Role | What it does |
|------|-------------|
| `sync_docker_stacks` | Rsync `docker/stacks/` (local) → `/opt/stacks` (DockerVM), excluding `data/` and `.env*`. Then writes `/opt/stacks/<stack>/.env` (0600) for each stack in the vault dict `docker_stack_env`; stacks not listed keep their `.env` untouched. Pull-back of remote-only stacks excludes `dockge/`, `data/` and `.env*`, gated by `sync_pull_remote` (default `true`; the control node sets it `false`). |
| `deploy_docker_stacks` | Runs `docker compose` per stack under `dockge_stacks_path` (skips `dockge`). `docker_state`: `up`, `down`, `restart`, `pull` (pull + `up -d --remove-orphans` + health) or `health` (wait until every container is running/healthy). `target_stack` (`""` = all). Results are recorded for the run summary (`pull`/`restart` success, any failure), then a failure fails the task. |
| `snapshot_proxmox_machines` | Snapshot `{{ snapshot_prefix }}_YYYYMMDD_HHMM` via the PVE API, keeps `snapshot_retention`. |
| `update_proxmox_machines` | `apt dist-upgrade` + autoremove, reboots if `/var/run/reboot-required` exists, records package count. |
| `update_pve_host` | Asserts a PVE repo is configured, `apt dist-upgrade`, never reboots; detects pending reboot (kernel, `needrestart -b`, reboot-required). |
| `preflight_backup_check` | Records warnings (never blocks) when a guest's newest PBS backup is missing, older than 36h, or the last vzdump task failed. |
| `pbs_server` | CIFS mount of the Synology share, datastore, `proxmox@pbs` user/ACL, prune/GC/verify jobs, Discord webhook notification. |
| `pve_backup_jobs` | PBS storage (fingerprint read from PBS), vzdump job (all except `backup_exclude_vmids`, snapshot, zstd), Discord webhook + matcher for backup failures. |
| `qemu_guest_agent` | Installs and starts `qemu-guest-agent` (needs `agent: 1` on the VM). |
| `control_node` | Provisions the control LXC; installs git/go-task, GitHub deploy key, clones the repo to `control_repo_path`, copies the vault, unattended-upgrades (reboot 06:00), systemd timers and notify unit. |
| `ssh_access` | Generates root's SSH key on the control node and authorizes it on all other hosts except `truenas`. |
| `update_truenas_apps` | TrueNAS apps via `midclt` over SSH (`update_truenas_apps_midclt`, default `midclt`): `catalog.sync` → `app.query` → per catalog app with an upgrade `app.upgrade_summary` → `truenas_upgrade_plan` (`filter_plugins/truenas_versions.py`, tests in `ansible/tests`) → `app.upgrade` to the highest non-major version (major = upstream or catalog major changes; `0.x` compares `0.minor`), with a host path snapshot (`update_truenas_apps_snapshot_hostpaths`), then waits until it is `RUNNING` again. Custom apps with image updates get `app.pull_images` (redeploy). One app at a time; a failed app is recorded (todo: `truenas:rollback`) and the next one runs. Majors and a pending OS update (`update.status`) are recorded as warnings; the OS is never updated. `update_truenas_apps_exclude` skips apps. In `include/truenas_update.yaml` it runs after `prune_truenas_images` and only when the free-space guard passed; images the upgrades make obsolete are pruned by the next week's run. |
| `prune_truenas_images` | Deletes unused Docker images on TrueNAS (the 25.10 API has no prune method): `app.image.query` + `app.query` (`active_workloads.images`) → `truenas_prune_plan` (`filter_plugins/truenas_prune.py`, tests in `ansible/tests`) → `app.image.delete <id> {"force": false}` (TrueNAS refuses images still in use). Per repository it keeps the images in use plus the `prune_truenas_images_rollback_count` (2) newest older ones (rollback); a repository no running app uses (an upgrade that left the app down, a removed app) keeps its 2 newest images and is listed as a warning; `prune_truenas_images_keep` (regexes) pins repositories. Apps that are not `RUNNING` have no containers, so their images come from `app.config` (`truenas_config_images`: `image` strings and `repository`/`tag` dicts; logged with `no_log`) and every image of those repositories is kept, even when a running app uses the same one (stopped custom app on `postgres:15`, running one on `postgres:17`). A stopped catalog app exposes no image anywhere in the 25.10 API (`app.get_instance` has no config, `version_details` or workloads), so its repository is only covered by the unused-repository rule; an image it shares with a running app's repository can be deleted (it is pulled again on start). Dangling images, older versions and newer unused images (failed upgrades) are deleted. Records one line (`pruned N images, freed X GB (free Y GB / Z % of the pool)`) plus warnings for unused repositories and failed deletes; the update run prunes once, before the upgrades, and sets `prune_truenas_images_report_ok: false`, so there the prune only shows up as a warning or a guard error. `tasks_from: guard` measures (`tasks/measure.yaml`, shared with the report) the free space of `prune_truenas_images_docker_path` (`/mnt/.ix-apps/docker`, where the images live) with `df` and the fill level of its pool with `zfs list` (`prune_truenas_images_zfs`, `/usr/sbin/zfs`; `df`'s size of a dataset is not the pool size), and sets `prune_truenas_images_space_ok`; below `prune_truenas_images_min_free_gb` (20, dataset) or `_min_free_pct` (10, pool) it records an error and the upgrades are skipped. Check mode lists what would be deleted. Never fails the play. `prune_truenas_images_enabled: false` skips it in the update run. |
| `run_report` | One summary per run. `tasks_from: record` (`report_level` ok/warn/error, `report_text`, `report_todo`, optional `report_host`) appends to the host fact `run_report_entries` and sends nothing; `tasks_from: send` (via `include/run_summary.yaml`) posts one message: worst level as color, one line per host, a Todo section, and implicit errors for hosts that dropped out. `run_report_notify: not_ok` skips green runs. |
| `notify_discord` | Only used by `run_report`, `rollback.yaml` and `truenas_rollback.yaml`. Posts one embed (`notify_title`, `notify_message`, `notify_level` info/warn/error, `notify_run_once`) to `discord_webhook_url`. Runs on localhost, never fails the play, skips when the URL is unset. |

### Docker Stacks (`docker/stacks/`)

Stacks are authored locally and deployed to `/opt/stacks` on the DockerVM via `sync_docker_stacks`. [Dockge](https://github.com/louislam/dockge) (at `docker/dockge/`) manages them on the VM side but is not itself a synced stack. All images are pinned to a version tag plus digest and bumped by Renovate (`renovate.json`; Grafana and Prometheus images are grouped, majors get `needs-review` and never automerge; `timescaledb-ha` (used by Scribe on Home Assistant) gets a PR only after approval in the Renovate Dependency Dashboard).

**observability** — Full metrics + logs pipeline:
- **Grafana** (`:3000`) — dashboards; anonymous viewer access enabled; embedding allowed.
- **Prometheus** (`:9090`) — metrics storage with remote-write receiver enabled. `prometheus.yaml` has `scrape_configs: []` — all scraping is done via Alloy.
- **Loki** (`:3100`) — log storage.
- **Alloy** (`:12345`) — collects metrics from node-exporter, logporter, and all observability containers; scrapes the Synology NAS node-exporter (10.0.40.10:9100); tails all Docker container logs and system logs (`/var/log/*.log`); forwards everything to local Prometheus/Loki. Alloy config is modular: `main.alloy` imports `modules/observability.alloy` and `modules/generic.alloy`.
- **node-exporter** + **logporter** (`:9333`) — host and per-container metrics (CPU, memory, disk I/O, network). logporter replaced cAdvisor, which used far more RAM and CPU; there is no per-container disk usage.- Grafana has the Strava datasource plugin preinstalled (`GF_PLUGINS_PREINSTALL`) for a planned Strava dashboard; it isn't used yet. The admin password comes from `GF_SECURITY_ADMIN_PASSWORD` in the stack's gitignored `.env`.

**roastbeef-swag** — Personal Discord bot (`ghcr.io/maribowman/roastbeef-swag`), port 8800, data persisted in `./data`.

**timescaledb** — TimescaleDB instance (config in `docker/stacks/timescaledb/`).

## Manual steps (cannot be automated)

- Authorize the laptop's `~/.ssh/id_ed25519.pub` for `root@pve` (`ssh-copy-id -i ~/.ssh/id_ed25519.pub root@10.0.10.10`). `pve` is a new SSH target, used by `update_pve_host` and `pve_backup_jobs`; before this branch Ansible only reached it through the API.
- Create the Discord webhook and add the vault keys (`ansible-vault edit ansible/inventory/group_vars/all/vault.yaml`).
- PVE API token `maintenance` privileges: VM.Snapshot, VM.Snapshot.Rollback, VM.Audit, VM.PowerMgmt, VM.Allocate, Sys.Audit, Datastore.Audit, Datastore.AllocateSpace, Datastore.AllocateTemplate.
- Pick the control LXC IP/vmid and fill in the `lxc-ansible` line under `[control_nodes]` in `hosts.ini`.
- Set `agent: 1` on vmid 101 (`qm set 101 --agent enabled=1`) and fully stop/start the VM.
- Install the Renovate GitHub App on `maribowman/homelab`; give it read access to the private `ghcr.io/maribowman/roastbeef-swag` package; require CI on `main` via branch protection.
- TrueNAS: enable the SSH service; create user `ansible` (home directory under a `home` dataset, e.g. `/mnt/MainStorage/home`) with the laptop and control-node public keys set in the UI (keys added to `authorized_keys` by hand are overwritten by the middleware), no sudo. `midclt` authenticates as that Unix user, so give its group a custom privilege (Credentials → Groups → Privileges) with only the roles `APPS_WRITE`, `CATALOG_WRITE` and `SYSTEM_UPDATE_READ`, not "Full Admin". Without a privilege every call fails with `ENOTAUTHENTICATED`. `update_truenas_apps_midclt` (host/group var, read by the rollback playbook too) can switch to `sudo -n midclt` instead. The firewall must allow control LXC → TrueNAS (22). The image prune needs no extra role (`app.image.query` and `app.config` are covered by `APPS_WRITE`, free space comes from plain `df` and `/usr/sbin/zfs list`); only if the `ansible` user can't `df /mnt/.ix-apps/docker` or `/usr/sbin/zfs list <pool>` add `DOCKER_READ` and `DATASET_READ`.
- TrueNAS half-pulled image layer (can't be automated): if an image pull keeps failing after space was freed, as an admin on TrueNAS find the partial image with `midclt call app.image.query`, remove it with `midclt call app.image.delete <id> '{"force": true}'`; if none is listed, `systemctl restart docker` (Docker drops incomplete downloads on restart). The free-space guard should make this rare.
- During the first `control:apply`, add the printed deploy key to GitHub (read-only).
- Inter-VLAN firewall: control LXC → `pve` (22, 8006), all 10.0.40.x hosts (22), github.com (22, 443), discord.com (443); `pve` → PBS (8007); PBS → Synology (445).
- PBS install and the Synology user/share permissions: see `proxmox-backup-server-setup.md`.

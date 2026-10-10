# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Personal homelab infrastructure managed with [Ansible](https://docs.ansible.com/) and [Taskfile](https://taskfile.dev/). The main host is a Proxmox server (`pve`, 10.0.10.10) running a DockerVM (`vm-docker`, 10.0.40.40), a Tailscale LXC (`lxc-tailscale`, 10.0.40.90), a Proxmox Backup Server LXC (`lxc-pbs`, 10.0.40.20) and a control LXC (`lxc-ansible`) that runs the playbooks on systemd timers. A Synology NAS is at 10.0.40.10 and holds the PBS datastore.

Git is the source of truth: edits made in the Dockge UI drift and get overwritten. TrueNAS and the Home Assistant instances are out of scope.

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
| Sun 04:00 | `git pull` → `proxmox:update` | `homelab-maintenance.timer` on `lxc-ansible` |
| Continuous | Renovate PRs; patch/minor/digest automerge when CI is green | GitHub (`renovate.json`, `.github/workflows/ci.yaml`) |

Both services share `/run/homelab.lock` and have `OnFailure=homelab-notify@%n.service`, which posts the last 30 journal lines to Discord.

## Architecture

### Ansible

- Venv lives at `ansible/.venv`; all ansible binaries must be invoked via that venv (the Taskfile handles this automatically).
- Secrets are stored in `ansible/inventory/group_vars/all/vault.yaml` (Ansible Vault). The vault password file is expected at `ansible/inventory/group_vars/all/.vault.pass` (gitignored, must be provisioned locally). Vault keys: `pve_token_secret`, `pbs_user_password` (`proxmox@pbs`), `synology_cifs_password`, `discord_webhook_url`.
- Inventory groups (`ansible/inventory/hosts.ini`): `proxmox_hosts`, `pbs_hosts`, `ubuntu_nodes`, `debian_nodes`, `docker_hosts`, `control_nodes`, and `guests` (= ubuntu + debian + pbs). Every host except `pve` has a `vmid`. `control_nodes` is deliberately not in `guests`; it updates itself via unattended-upgrades.
- Shared vars are in `ansible/inventory/group_vars/all/vars.yaml`: Proxmox API details, PBS/NAS addresses, backup schedule and retention, snapshot prefix/retention, timer `OnCalendar` values, control-node repo settings, `dockge_stacks_path` (`/opt/stacks` on DockerVM). Role-specific vars live in each role's `defaults/main.yaml`, prefixed with the role name.
- Collections used: `community.proxmox`, `community.general`, `ansible.posix`.
- CI (`.github/workflows/ci.yaml`) runs yamllint, ansible-lint (profile `production`), `--syntax-check` of every `ansible/playbooks/*.yaml` with a stub vault, and `docker compose config -q` per stack.

### Playbooks

| Playbook | What it does |
|---|---|
| `proxmox.yaml` | Preflight backup check → guests `serial: 1` (snapshot, docker down, apt, reboot, docker up + health) → PBS → PVE host (upgrade, never reboots, warns when a reboot is pending) → one Discord summary. A failed guest stops all further updates (`not_run`) and the run fails after the summary. |
| `rollback.yaml` | `-e target=<host>` [`-e snapshot=<name>`]: rolls back to the newest `pre_update_*` snapshot and starts the guest. |
| `docker.yaml` | `sync_docker_stacks` (tag `sync`) then `deploy_docker_stacks` (tag `deploy`). |
| `backup.yaml` | `pbs_server` on PBS → `pve_backup_jobs` on PVE → `qemu_guest_agent` on Ubuntu VMs. Idempotent: reads JSON state first, changes only on a diff. |
| `control.yaml` | Play 1 (tag `provision`): create the control LXC. Play 2 (tag `apply`): `ssh_access` + `control_node`. |

### Ansible Roles

| Role | What it does |
|------|-------------|
| `sync_docker_stacks` | Rsync `docker/stacks/` (local) → `/opt/stacks` (DockerVM), excluding `data/`. Pull-back of remote-only stacks excludes `dockge/` and `data/`, gated by `sync_pull_remote` (default `true`; the control node sets it `false`). |
| `deploy_docker_stacks` | Runs `docker compose` per stack under `dockge_stacks_path` (skips `dockge`). `docker_state`: `up`, `down`, `restart`, `pull` (pull + `up -d --remove-orphans` + health) or `health` (wait until every container is running/healthy). `target_stack` (`""` = all). Failures → one Discord error, then fail. |
| `snapshot_proxmox_machines` | Snapshot `{{ snapshot_prefix }}_YYYYMMDD_HHMM` via the PVE API, keeps `snapshot_retention`. |
| `update_proxmox_machines` | `apt dist-upgrade` + autoremove, reboots if `/var/run/reboot-required` exists, records package count. |
| `update_pve_host` | Asserts a PVE repo is configured, `apt dist-upgrade`, never reboots; detects pending reboot (kernel, `needrestart -b`, reboot-required). |
| `preflight_backup_check` | Warns (never blocks) when a guest's newest PBS backup is missing, older than 36h, or the last vzdump task failed. |
| `pbs_server` | CIFS mount of the Synology share, datastore, `proxmox@pbs` user/ACL, prune/GC/verify jobs, Discord webhook notification. |
| `pve_backup_jobs` | PBS storage (fingerprint read from PBS), vzdump job (all except `backup_exclude_vmids`, snapshot, zstd), Discord webhook + matcher for backup failures. |
| `qemu_guest_agent` | Installs and starts `qemu-guest-agent` (needs `agent: 1` on the VM). |
| `control_node` | Provisions the control LXC; installs git/go-task, GitHub deploy key, clones the repo to `control_repo_path`, copies the vault, unattended-upgrades (reboot 06:00), systemd timers and notify unit. |
| `ssh_access` | Generates root's SSH key on the control node and authorizes it on all other hosts. |
| `notify_discord` | Posts one embed (`notify_title`, `notify_message`, `notify_level` info/warn/error, `notify_run_once`) to `discord_webhook_url`. Runs on localhost, never fails the play, skips when the URL is unset. |

### Docker Stacks (`docker/stacks/`)

Stacks are authored locally and deployed to `/opt/stacks` on the DockerVM via `sync_docker_stacks`. [Dockge](https://github.com/louislam/dockge) (at `docker/dockge/`) manages them on the VM side but is not itself a synced stack. All images are pinned to a version tag plus digest and bumped by Renovate (`renovate.json`; Grafana and Prometheus images are grouped, majors and `timescaledb-ha` get `needs-review` and never automerge).

**observability** — Full metrics + logs pipeline:
- **Grafana** (`:3000`) — dashboards; anonymous viewer access enabled; embedding allowed.
- **Prometheus** (`:9090`) — metrics storage with remote-write receiver enabled. `prometheus.yaml` has `scrape_configs: []` — all scraping is done via Alloy.
- **Loki** (`:3100`) — log storage.
- **Alloy** (`:12345`) — collects metrics from node-exporter, cAdvisor, and all observability containers; scrapes the Synology NAS node-exporter (10.0.40.10:9100); tails all Docker container logs and system logs (`/var/log/*.log`); forwards everything to local Prometheus/Loki. Alloy config is modular: `main.alloy` imports `modules/observability.alloy` and `modules/generic.alloy`.
- **node-exporter** + **cAdvisor** — host and container metrics. cAdvisor has several expensive metric collectors disabled.

**roastbeef-swag** — Personal Discord bot (`ghcr.io/maribowman/roastbeef-swag`), port 8800, data persisted in `./data`.

**timescaledb** — TimescaleDB instance (config in `docker/stacks/timescaledb/`).

## Manual steps (cannot be automated)

- Create the Discord webhook and add the vault keys (`ansible-vault edit ansible/inventory/group_vars/all/vault.yaml`).
- PVE API token `maintenance` privileges: VM.Snapshot, VM.Snapshot.Rollback, VM.Audit, VM.PowerMgmt, VM.Allocate, Sys.Audit, Datastore.Audit, Datastore.AllocateSpace, Datastore.AllocateTemplate.
- Pick the control LXC IP/vmid and fill in the `lxc-ansible` line under `[control_nodes]` in `hosts.ini`.
- Set `agent: 1` on vmid 101 (`qm set 101 --agent enabled=1`) and fully stop/start the VM.
- Install the Renovate GitHub App on `maribowman/homelab`; give it read access to the private `ghcr.io/maribowman/roastbeef-swag` package; require CI on `main` via branch protection.
- During the first `control:apply`, add the printed deploy key to GitHub (read-only).
- Inter-VLAN firewall: control LXC → `pve` (22, 8006), all 10.0.40.x hosts (22), github.com (22, 443), discord.com (443); `pve` → PBS (8007); PBS → Synology (445).
- PBS install and the Synology user/share permissions: see `proxmox-backup-server-setup.md`.

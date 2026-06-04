# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Personal homelab infrastructure managed with [Ansible](https://docs.ansible.com/) and [Taskfile](https://taskfile.dev/). The main host is a Proxmox server running a DockerVM (`vm-docker`, 192.168.0.69) and a Tailscale LXC (`lxc-tailscale`, 192.168.0.155). A Synology NAS is at 192.168.0.3.

## Common Commands

All commands use `task` (Taskfile runner). Dependencies are bootstrapped automatically — `task ansible:init` is a dep of every ansible task and only runs when `requirements.txt`/`requirements.yaml` change.

```sh
# Docker
task ansible:docker:deploy          # Rsync stacks to DockerVM and bring all stacks up
task ansible:docker:restart STACK=observability  # Restart a single stack (observability | roastbeef-swag | timescaledb)

# Proxmox
task ansible:proxmox:update         # Snapshot guests (by vmid), apt upgrade + reboot guests, then patch/reboot the PVE host last
task ansible:proxmox:syntax         # Syntax-check the proxmox playbook
task ansible:proxmox:ping           # Test SSH connectivity to all hosts
```

## Architecture

### Ansible

- Venv lives at `ansible/.venv`; all ansible binaries must be invoked via that venv (the Taskfile handles this automatically).
- Secrets are stored in `ansible/inventory/group_vars/all/vault.yaml` (Ansible Vault). The vault password file is expected at `ansible/inventory/group_vars/all/.vault.pass` (gitignored, must be provisioned locally).
- Inventory vars are in `ansible/inventory/group_vars/all/vars.yaml`: Proxmox API details, `dockge_stacks_path` (`/opt/stacks` on DockerVM), `retention_days`.
- Collections used: `community.proxmox`, `community.general`, `ansible.posix`.

### Ansible Roles

| Role | What it does |
|------|-------------|
| `sync_docker_stacks` | Bidirectional rsync between `docker/stacks/` (local) and `/opt/stacks` (DockerVM). Push excludes `data/`; pull excludes `dockge/`. |
| `deploy_docker_stacks` | Finds all compose files under `dockge_stacks_path`, runs `docker compose <up -d \| down \| restart>` per stack. Skips the `dockge` stack itself. Controlled by `docker_state` (default: `restart`) and `target_stack` (default: `""` = all stacks). |
| `snapshot_proxmox_machines` | Creates a pre-update snapshot via Proxmox API (`proxmox_snap`), retains last 2. |
| `update_proxmox_machines` | `apt dist-upgrade`, reboots if `/var/run/reboot-required` exists. |

### Docker Stacks (`docker/stacks/`)

Stacks are authored locally and deployed to `/opt/stacks` on the DockerVM via `sync_docker_stacks`. [Dockge](https://github.com/louislam/dockge) (at `docker/dockge/`) manages them on the VM side but is not itself a synced stack.

**observability** — Full metrics + logs pipeline:
- **Grafana** (`:3000`) — dashboards; anonymous viewer access enabled; embedding allowed.
- **Prometheus** (`:9090`) — metrics storage with remote-write receiver enabled. `prometheus.yaml` has `scrape_configs: []` — all scraping is done via Alloy.
- **Loki** (`:3100`) — log storage.
- **Alloy** (`:12345`) — collects metrics from node-exporter, cAdvisor, and all observability containers; scrapes the Synology NAS node-exporter (192.168.0.3:9100); tails all Docker container logs and system logs (`/var/log/*.log`); forwards everything to local Prometheus/Loki. Alloy config is modular: `main.alloy` imports `modules/observability.alloy` and `modules/generic.alloy`.
- **node-exporter** + **cAdvisor** — host and container metrics. cAdvisor has several expensive metric collectors disabled.

**roastbeef-swag** — Personal Discord bot (`ghcr.io/maribowman/roastbeef-swag`), port 8800, data persisted in `./data`.

**timescaledb** — TimescaleDB instance (config in `docker/stacks/timescaledb/`).

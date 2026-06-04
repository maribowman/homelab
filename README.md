# Homelab

Personal Homelab infrastructure managed with [Ansible](https://docs.ansible.com/) and [Taskfiles](https://taskfile.dev/).

## Docker stacks

### Observability

- Logs are scraped from all Docker containers on the [Proxmox](https://proxmox.com/) DockerVM
- Metrics are scraped from the Proxmox DockerVM, all Observability stack containers and the Synology NAS

All metrics and logs are visualized in [Grafana](https://grafana.com/oss/grafana/) dashboards.
Requires a gitignored `.env` with `GF_SECURITY_ADMIN_PASSWORD` (see `.env.example`).

### Roastbeef-Swag

Personal Discord Bot written in [go](https://go.dev/).

### TimescaleDB

[TimescaleDB](https://www.timescale.com/) instance used by Home Assistant. Requires a gitignored
`.env` with `POSTGRES_PASSWORD` (see `.env.example`).

## Backups

Proxmox guests are backed up with [Proxmox Backup Server](https://www.proxmox.com/en/products/proxmox-backup-server)
running as an LXC, storing to the Synology NAS over CIFS. See
[proxmox-backup-server-setup.md](./proxmox-backup-server-setup.md).

## Automation

### Proxmox

```sh
task ansible:proxmox:update   # Snapshot guests, upgrade packages + reboot guests, then patch/reboot the PVE host last
task ansible:proxmox:syntax   # Syntax-check the proxmox playbook
task ansible:proxmox:ping     # Test SSH connectivity to all hosts
```

### Docker

```sh
task ansible:docker:deploy                       # Sync stacks from repo to DockerVM and bring all stacks up
task ansible:docker:restart STACK=observability  # Restart a single stack (observability | roastbeef-swag | timescaledb)
```

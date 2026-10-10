# Homelab

Personal Homelab infrastructure managed with [Ansible](https://docs.ansible.com/) and [Taskfiles](https://taskfile.dev/).

## Docker stacks

### Observability

- Logs are scraped from all Docker containers on the [Proxmox](https://proxmox.com/) DockerVM
- Metrics are scraped from the Proxmox DockerVM, all Observability stack containers and the Synology NAS

All metrics and logs are visualized in [Grafana](https://grafana.com/oss/grafana/) dashboards.

### Roastbeef-Swag

Personal Discord Bot written in [go](https://go.dev/).

## Automation

Nothing is updated by hand any more. A control LXC (`lxc-ansible`) runs the playbooks from this repo on systemd timers, and every run reports to Discord:

| When | What |
|---|---|
| Daily 01:00 | Native PVE backup of all guests to PBS (datastore on the Synology); PBS prunes, garbage-collects and verifies |
| Daily 05:00 | `git pull`; redeploy the Docker stacks if the repo changed |
| Sun 04:00 | Snapshot → update every guest one at a time (PBS last) → health check → upgrade the PVE host (never rebooted automatically) → summary |
| Continuous | Renovate PRs for image updates; patch/minor automerge when CI is green |

### Proxmox

```sh
task ansible:proxmox:update                      # Full maintenance run
task ansible:proxmox:update -- --limit lxc-tailscale
task ansible:proxmox:check                       # Dry-run (--check --diff)
task ansible:proxmox:rollback HOST=lxc-tailscale # Roll back to the newest pre-update snapshot
task ansible:proxmox:ping                        # Test SSH connectivity to all hosts
```

### Backups

```sh
task ansible:backup:check   # Show what would change on PBS/PVE
task ansible:backup:apply   # Configure PBS and the PVE backup job
```

Manual one-time steps are in [proxmox-backup-server-setup.md](proxmox-backup-server-setup.md).

### Docker

```sh
task ansible:docker:deploy                        # Sync stacks, pull pinned images, up -d, health check
task ansible:docker:restart STACK=observability
```

### Control node

```sh
task ansible:control:provision   # Create the LXC
task ansible:control:apply       # Configure it (from the laptop)
```

See [AGENTS.md](AGENTS.md) for the roles, vars, vault keys and the manual steps.


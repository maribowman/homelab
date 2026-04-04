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

### Proxmox

```sh
task ansible:proxmox:update   # Snapshot VMs, upgrade packages, reboot if needed
task ansible:proxmox:syntax   # Syntax-check the proxmox playbook
task ansible:proxmox:ping     # Test SSH connectivity to all hosts
```

### Docker

```sh
task ansible:docker:deploy    # Sync stacks from repo to DockerVM and reload Docker stacks
```

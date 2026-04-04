# homelab

Personal homelab infrastructure managed with Ansible.

## Docker stacks

### Observability

Grafana Alloy collects:

- Logs from all Docker containers on the Proxmox DockerVM
- System logs from Proxmox DockerVM
- Metrics from node-exporter, cAdvisor, Prometheus, Loki, Grafana, and Alloy itself
- Metrics from Synology NAS

All metrics are remote-written to Prometheus. Logs are pushed to Loki. Both are visualized in Grafana.

## Automation

Tasks are run via [Task](https://taskfile.dev). Dependencies (Python venv + Ansible collections) are automatically installed and updated.

```
task --list
```

### Proxmox

```sh
task ansible:proxmox:update   # Snapshot VMs, upgrade packages, reboot if needed
task ansible:proxmox:syntax   # Syntax-check the proxmox playbook
task ansible:proxmox:ping     # Test SSH connectivity to all hosts
```

### Docker

```sh
task ansible:docker:deploy    # Sync stacks from repo to VM and run docker compose up
```

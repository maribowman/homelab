# Homelab

Personal Homelab infrastructure managed with [Ansible](https://docs.ansible.com/) and [Taskfiles](https://taskfile.dev/).

## Docker stacks

### Observability

- Logs are scraped from all Docker containers on the [Proxmox](https://proxmox.com/) DockerVM
- Metrics are scraped from the Proxmox DockerVM, all Observability stack containers and the Synology NAS

All metrics and logs are visualized in [Grafana](https://grafana.com/oss/grafana/) dashboards.

### Roastbeef-Swag

Personal Discord Bot written in [go](https://go.dev/).

### TimescaleDB

Home Assistant sensor data history.

## Automation

Nothing is updated by hand any more. A control LXC (`lxc-ansible`) runs the playbooks from this repo on systemd timers. Every run ends in one Discord summary whose color shows the worst result: green when all is good, yellow when it is done but with a todo that can wait (e.g. reboot the PVE host), red when something failed and needs a manual fix (the message lists what to do, e.g. the rollback command). The daily deploy only posts when it is not green:

| When        | What                                                                                                                                  |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Daily 01:00 | Native PVE backup of all guests to PBS (datastore on the Synology); PBS prunes, garbage-collects and verifies                         |
| Daily 05:00 | `git pull`; redeploy the Docker stacks if the repo changed                                                                            |
| Sun 04:00   | Snapshot → update every guest one at a time (PBS last) → health check → upgrade the PVE host (never rebooted automatically) → upgrade TrueNAS apps (non-major only; majors and TrueNAS OS updates become todos) → summary |
| Continuous  | Renovate PRs for image updates; patch/minor automerge when CI is green                                                                |

## Setup

### Ansible vault

Secrets live in `ansible/inventory/group_vars/all/vault.yaml`, encrypted with the password in `.vault.pass` next to it. Both files are gitignored and exist only on the laptop; `task ansible:control:apply` copies them to the control node. Create them once:

```sh
echo '<vault password>' > ansible/inventory/group_vars/all/.vault.pass
chmod 600 ansible/inventory/group_vars/all/.vault.pass

# use `create` instead of `edit` the first time
ansible/.venv/bin/ansible-vault edit ansible/inventory/group_vars/all/vault.yaml \
  --vault-password-file ansible/inventory/group_vars/all/.vault.pass
```

The vault holds these keys:

```yaml
pve_token_secret: ... # PVE API token root@pam!maintenance
pbs_user_password: ... # proxmox@pbs
synology_cifs_password: ... # Synology share for the PBS datastore
discord_webhook_url: ...
docker_stack_env: # one dict per stack, rendered to /opt/stacks/<stack>/.env
  observability:
    GF_SECURITY_ADMIN_PASSWORD: ...
  timescaledb:
    POSTGRES_PASSWORD: ...
```

After changing the vault, run `task ansible:control:apply` so the control node gets the new copy.

### Stack `.env` files

`.env` files are never kept in the repo or synced from it. `task ansible:docker:deploy` writes `/opt/stacks/<stack>/.env` on the DockerVM from `docker_stack_env` in the vault, so every key a stack needs must be in the vault (values must not contain a single quote). Stacks not listed there keep their existing `.env`.

Other one-time steps (SSH keys, API token privileges, firewall, Renovate) are listed in [AGENTS.md](AGENTS.md#manual-steps-cannot-be-automated) and [proxmox-backup-server-setup.md](proxmox-backup-server-setup.md).

## Commands

Dependencies (venv, collections) are installed automatically on first use. Extra Ansible args go after `--`, e.g. `task ansible:proxmox:update -- --limit lxc-tailscale`.

| Command                                     | Description                                                                 |
| ------------------------------------------- | --------------------------------------------------------------------------- |
| `task ansible:proxmox:update`               | Full maintenance run: snapshot, update guests one by one, PVE host, summary |
| `task ansible:proxmox:check`                | Dry-run of the above (`--check --diff`)                                     |
| `task ansible:proxmox:rollback HOST=<host>` | Roll a guest back to its newest pre-update snapshot                         |
| `task ansible:proxmox:ping`                 | Test SSH connectivity to all hosts                                          |
| `task ansible:proxmox:syntax`               | Syntax-check the Proxmox playbooks                                          |
| `task ansible:maintenance:run`             | The Sunday run: Proxmox update, then TrueNAS apps, one summary              |
| `task ansible:maintenance:check`           | Dry-run of the above                                                        |
| `task ansible:truenas:update`              | Upgrade TrueNAS apps to their newest non-major version, summary             |
| `task ansible:truenas:check`               | Dry-run of the above (shows the plans, upgrades nothing)                    |
| `task ansible:truenas:rollback APP=<app>`  | Roll a TrueNAS app back (optional `VERSION=<v>`), with its snapshot         |
| `task ansible:truenas:syntax`              | Syntax-check the TrueNAS playbooks                                          |
| `task ansible:docker:deploy`                | Sync stacks, write `.env` files, pull pinned images, `up -d`, health check  |
| `task ansible:docker:restart STACK=<stack>` | Restart a single stack                                                      |
| `task ansible:backup:apply`                 | Configure PBS (datastore, jobs, notifications) and the PVE backup job       |
| `task ansible:backup:check`                 | Dry-run of the above                                                        |
| `task ansible:backup:syntax`                | Syntax-check the backup playbook                                            |
| `task ansible:control:provision`            | Create the control LXC                                                      |
| `task ansible:control:apply`                | Configure the control LXC (run from the laptop)                             |
| `task ansible:control:syntax`               | Syntax-check the control playbook                                           |

See [AGENTS.md](AGENTS.md) for the roles, vars and architecture.

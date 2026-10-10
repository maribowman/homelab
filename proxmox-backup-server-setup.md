# Proxmox Backup Server (PBS) Setup with Synology NAS

## Overview

Proxmox Backup Server (PBS) runs as an LXC container (vmid `1001`) on Proxmox VE (PVE) and stores backups on a Synology NAS via CIFS/SMB.

Almost everything is configured as code by `ansible/playbooks/backup.yaml`:

| Role | Host | What it configures |
| ---- | ---- | ------------------ |
| [`pbs_server`](ansible/roles/pbs_server/) | `pbs_hosts` | `cifs-utils`, `/etc/pbs-synology-credentials`, the fstab mount of `//nas_host/nas_share`, the `pbs_datastore` datastore (GC daily, verify-new), the `proxmox@pbs` user with `DatastoreAdmin` on `/datastore`, the prune job (`pbs_keep_*`), the verify job, and a Discord webhook + matcher for job errors |
| [`pve_backup_jobs`](ansible/roles/pve_backup_jobs/) | `proxmox_hosts` | The `pve_pbs_storage_id` PBS storage (fingerprint read from PBS, "keep all" retention), the vzdump job (all guests except `backup_exclude_vmids`, snapshot mode, zstd, `backup_schedule`), and a Discord webhook + matcher for failed backups |
| [`qemu_guest_agent`](ansible/roles/qemu_guest_agent/) | `ubuntu_nodes` | Installs and starts `qemu-guest-agent` (consistent snapshot-mode backups of VMs) |

```sh
task ansible:backup:check    # dry run with diff
task ansible:backup:apply    # apply
task ansible:backup:syntax   # syntax check
```

All values (IPs, datastore, schedule, retention) live in `ansible/inventory/group_vars/all/vars.yaml` and the role `defaults/`. Secrets come from the vault: `synology_cifs_password`, `pbs_user_password`, `discord_webhook_url`.

> PBS is excluded from its own backup job to prevent a deadlock where PBS tries to back up itself while writing backups. Retention is managed only on the PBS side (prune job); the PVE storage keeps all backups.

The steps below cannot be automated and are needed once, before the first `backup:apply`.

---

## 1. Install PBS

Install PBS via [community-scripts](https://community-scripts.org/scripts/proxmox-backup-server) and run its post-install script. The LXC must be able to mount CIFS shares (privileged container, or the `mount=cifs` feature).

---

## 2. Synology user and share

- Enable SMB: Control Panel → File Services → SMB.
- Create a dedicated `proxmox-backup-server` OS user; store its password in the vault as `synology_cifs_password`.
- Create a shared folder (`backups`, see `nas_share`) with a subfolder for PBS (`proxmox_backup_home`, see `nas_subdir`).

### Permissions

CIFS mounts only the top-level shared folder, so permissions are set at two levels:

- **Shared folder level** (`backups`): grant `proxmox-backup-server` **Read Only**
  - Control Panel → Shared Folder → Edit → Permissions
- **Subfolder level** (`proxmox_backup_home`): grant **Read/Write**
  - Enable advanced permissions: Shared Folder → Edit → Advanced → Enable advanced share permissions
  - File Station → right-click `proxmox_backup_home` → Properties → Permission

> The subdirectory cannot be part of the CIFS mount path, only the top-level share can be mounted. The subdirectory is selected as the datastore path instead (`/mnt/proxmox_backup_home/proxmox_backup_home`).

---

## 3. Vault keys

```sh
ansible-vault edit ansible/inventory/group_vars/all/vault.yaml
```

| Key | Used for |
| --- | -------- |
| `synology_cifs_password` | CIFS credentials file on PBS |
| `pbs_user_password` | `proxmox@pbs`; set when the user is created and used by the PVE storage. Changing it later updates PVE, but the PBS user's password must be changed by hand. |
| `discord_webhook_url` | PVE/PBS notification webhooks (skipped when unset) |

---

## 4. Useful checks

```bash
# PBS
df -h | grep proxmox
proxmox-backup-manager datastore list
proxmox-backup-manager prune-job list
proxmox-backup-manager verify-job list

# PVE
pvesm status
pvesh get /cluster/backup
```

Check the deduplication factor on the PBS datastore summary page after a few backups.

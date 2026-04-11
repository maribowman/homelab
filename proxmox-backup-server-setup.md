# Proxmox Backup Server (PBS) Setup with Synology NAS

## Overview

This guide documents the setup of Proxmox Backup Server (PBS) running as an LXC container on Proxmox VE (PVE), storing backups on a Synology NAS via CIFS/SMB.

---

## 1. Prerequisites

- PBS installed via [community-scripts](https://community-scripts.org/scripts/proxmox-backup-server) and post-install script run
- Synology NAS with:
  - A dedicated shared folder (e.g. `backups`) containing a subfolder for PBS (e.g. `proxmox_backup_home`)
  - A dedicated `proxmox-backup-server` OS user created
  - SMB enabled (Control Panel → File Services → SMB)

---

## 2. Synology Permissions

Since CIFS mounts only the top-level shared folder, permissions must be set at two levels:

- **Shared folder level** (`backups`): grant `proxmox-backup-server` user **Read Only**
  - Control Panel → Shared Folder → Edit → Permissions
- **Subfolder level** (`proxmox_backup_home`): grant **Read/Write**
  - Enable advanced permissions: Shared Folder → Edit → Advanced → Enable advanced share permissions
  - File Station → right-click `proxmox_backup_home` → Properties → Permission

---

## 3. Mount Synology Share on PBS Host

SSH into the PBS host and run:

```bash
apt install cifs-utils -y
mkdir -p /mnt/proxmox_backup_home
vim /etc/pbs-synology-credentials
```

Contents of `/etc/pbs-synology-credentials`:

```
username=proxmox-backup-server
password=YOUR_PASSWORD
```

Secure the credentials file:

```bash
chmod 600 /etc/pbs-synology-credentials
```

Add to `/etc/fstab`:

```
//NAS_IP/backups /mnt/proxmox_backup_home cifs credentials=/etc/pbs-synology-credentials,vers=2.0,uid=backup,gid=backup,iocharset=utf8 0 0
```

Mount and verify:

```bash
systemctl daemon-reload
mount -a
df -h | grep proxmox
```

> **Note:** The subdirectory (`proxmox_backup_home`) cannot be part of the CIFS mount path — only the top-level share (`backups`) can be mounted. The subdirectory is instead specified at the PBS Datastore level.

---

## 4. Create PBS Datastore

1. Open PBS web UI at `https://PBS_IP:8007`
2. Go to **Datastore → Add Datastore**
3. Set:
   - **Name**: `synology-backup`
   - **Backing Path**: `/mnt/proxmox_backup_home/proxmox_backup_home`
   - **GC Schedule**: `daily`
   - **Prune Schedule**: `daily`

---

## 5. Configure Retention on PBS

In PBS: **Datastore → synology-backup → Prune & GC Jobs → Edit**

| Setting      | Value |
| ------------ | ----- |
| Keep Last    | 3     |
| Keep Weekly  | 3     |
| Keep Monthly | 3     |

> Retention is managed exclusively on the PBS side. On the PVE storage definition, "Keep all backups" is checked to avoid conflicts.

---

## 6. PBS Access Control

A dedicated `proxmox@pbs` user is created in PBS with the `DatastoreAdmin` role scoped to `/datastore` (with propagation enabled).

In PBS: **Configuration → Access Control → Permissions → Add**

- **Path**: `/datastore`
- **User**: `proxmox@pbs`
- **Role**: `DatastoreAdmin`
- **Propagate**: Yes

---

## 7. Add PBS as Storage in Proxmox VE

Get the PBS fingerprint first:

```bash
proxmox-backup-manager cert info | grep Fingerprint
```

In PVE: **Datacenter → Storage → Add → Proxmox Backup Server**

| Field            | Value                             |
| ---------------- | --------------------------------- |
| ID               | `pbs-synology`                    |
| Server           | `192.168.0.101`                   |
| Username         | `proxmox@pbs`                     |
| Datastore        | `synology-backup`                 |
| Fingerprint      | _(output from above command)_     |
| Backup Retention | Keep all backups (managed by PBS) |

---

## 8. Create Backup Job in PVE

In PVE: **Datacenter → Backup → Add**

| Field          | Value                                         |
| -------------- | --------------------------------------------- |
| Node           | `pve`                                         |
| Storage        | `pbs-synology`                                |
| Schedule       | `01:00`                                       |
| Selection Mode | Exclude selected VMs                          |
| Excluded       | `1001` (PBS LXC — excluded to avoid deadlock) |
| Mode           | Snapshot                                      |
| Compression    | ZSTD                                          |

> PBS is excluded from its own backup job to prevent a deadlock situation where PBS tries to back up itself while actively writing backups.

---

## 9. Additional Settings

- **Verify new snapshots**: Enabled in PBS — automatically verifies backup integrity after each write
- **Deduplication**: PBS deduplicates chunks across backups, significantly reducing actual storage usage. Check the Deduplication Factor on the PBS Datastore Summary page after a few backups.

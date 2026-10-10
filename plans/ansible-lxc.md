# Control LXC (`lxc-ansible`) setup guide

> **For Claude:** I'm setting up the control node for my homelab automation. Walk me through the steps below **one at a time**. Before each step, tell me what it does. After it, ask for the output and check it against "Expected" before we move on. If something fails, help me debug it before continuing. Don't skip the "Decide" items: ask me. I run everything from my laptop, in the repo root (`~/Dev/homelab`).

## Context: what exists and why

My homelab is one Proxmox host (`pve`, 10.0.10.10). It runs these guests:

| Host | IP | vmid | Role |
|---|---|---|---|
| `vm-docker` | 10.0.40.40 | 101 | Docker VM: observability, roastbeef-swag (Discord bot), TimescaleDB (used by Scribe on Home Assistant) |
| `lxc-tailscale` | 10.0.40.90 | 1000 | Tailscale |
| `lxc-pbs` | 10.0.40.20 | 1001 | Proxmox Backup Server; its datastore is on the Synology NAS (10.0.40.10) |

The VLANs are 10.0.10.x (management) and 10.0.40.x (services). My laptop is on 10.0.20.x.

The repo is `git@github.com:maribowman/homelab.git`. Everything is Ansible, run through `task` (go-task). Git is the source of truth. Secrets are in Ansible Vault: `ansible/inventory/group_vars/all/vault.yaml`, with its password file `.vault.pass` next to it (gitignored). Vault keys: `pve_token_secret`, `pbs_user_password`, `synology_cifs_password`, `discord_webhook_url`.

The branch `feat/update-and-automation` added these:

- **`proxmox:update`:** weekly maintenance.
  1. Warns if a guest has no recent backup.
  2. Snapshots each guest (`pre_update_YYYYMMDD_HHMM`, keeps 2) and updates the guests one at a time with `apt`, rebooting if needed. PBS goes last. On vm-docker the Docker stacks are stopped first, then started and health-checked.
  3. Upgrades the PVE host but never reboots it; warns when a reboot is pending.
  4. Sends one Discord summary.
  5. A failed guest stops all further updates. The run then fails, which triggers the systemd failure alert below.
- **`proxmox:rollback HOST=…`:** restores a guest's newest pre-update snapshot.
- **`backup:apply`:** PBS datastore and jobs, plus the PVE backup job, as code.
- **`docker:deploy`:** rsyncs `docker/stacks/` to the VM, pulls the pinned images, runs `up -d`, then a health check. Renovate opens image-update PRs; TimescaleDB updates need my approval in the Dependency Dashboard.
- **`control:provision` / `control:apply`:** this guide. They create a small Debian 13 LXC that runs the above on a schedule, so nothing depends on my laptop being on.

**Why a control LXC:** the automation runs inside the homelab, on systemd timers. It pulls the repo from GitHub, so merging to `main` is the deploy. It reports to Discord. My laptop is only needed for the one-time setup and for manual runs.

### What the control node does once it's running

| Timer | When | What |
|---|---|---|
| `homelab-deploy.timer` | daily 05:00 | `git pull`. If HEAD differs from the last deployed commit, it runs `docker:deploy` with `sync_pull_remote=false`. A failed deploy is retried the next day. |
| `homelab-maintenance.timer` | Sun 04:00 | `git pull`, then `proxmox:update` |
| (native PVE/PBS jobs) | daily 01:00 | backups. These don't depend on the control node. |

- **Locking:** both services take `flock /run/homelab.lock`, so they never overlap.
- **Failure alert:** on failure, `homelab-notify@.service` posts the last 30 journal lines to Discord.
- **Self-updates:** the LXC updates itself with unattended-upgrades and reboots at 06:00 when needed. It is not in the `guests` group.

### What the two playbook steps do

**`task ansible:control:provision`** (play 1). Runs on the laptop against the PVE API with the `maintenance` token:
- downloads the Debian template `debian-13-standard_13.1-2_amd64.tar.zst`
- creates an unprivileged LXC: 1 core, 1 GB RAM, 8 GB on `local-lvm`, `vmbr0`, VLAN tag 40, gateway 10.0.40.1, `nesting=1`, start on boot
- injects my `~/.ssh/id_ed25519.pub`, starts the container, and waits for SSH

**`task ansible:control:apply`** (play 2). Runs over SSH:
- `ssh_access` generates root's SSH key on the LXC and authorizes it on every other host, `pve` included.
- `control_node` then does the rest:
  - installs git, python3-venv, rsync, go-task 3.54.0 (`.deb` from GitHub) and unattended-upgrades
  - sets the timezone to Europe/Berlin
  - generates a GitHub deploy key and **pauses until I've added it to GitHub**
  - clones the repo to `/opt/homelab` (branch `main`) and copies `vault.yaml` and `.vault.pass` from my laptop into it
  - writes the Discord env file to `/etc/homelab`
  - installs the units and scripts, then enables both timers

The defaults are in `ansible/roles/control_node/defaults/main.yaml` and can be overridden with `-e`.

## Prerequisites (check before starting)

- [ ] The rest of the branch is tested from the laptop: `proxmox:check`, a real `proxmox:update` limited to one guest, and `backup:check`.
- [ ] **The branch is merged into `main`.** The LXC clones `main`. Until then, add `-e control_node_repo_version=feat/update-and-automation` to `control:apply`, and switch the clone back later.
- [ ] `task ansible:proxmox:ping` succeeds for all hosts.
- [ ] The vault has a working `pve_token_secret` and `discord_webhook_url`.
- [ ] The `maintenance` API token (`root@pam!maintenance`) can create containers: VM.Allocate, VM.PowerMgmt, VM.Audit, Datastore.AllocateSpace and Datastore.AllocateTemplate (template download), plus Sys.Audit. If the token is privilege-separated, PVE 8+ may also need **SDN.Use** on `/sdn/zones/localnetwork` for the `vmbr0` bridge.
- [ ] The Renovate GitHub App is installed, and CI is required on `main`. Optional for the LXC itself, but it's what makes "merge = deploy" safe.

## Steps

### 1. Decide the IP and vmid
- **Decide:** a free IP in 10.0.40.x and a free vmid. Check with `pct list` / `qm list` on `pve`, and against the router's DHCP and static leases.
- Is `vmbr0` VLAN-aware with tag 40, as `vm-docker` and `lxc-pbs` use? Check it on `pve`:
  ```sh
  pct config 1000 | grep net0
  ```
  If they don't use a tag, I need `-e control_node_lxc_vlan_tag=""`, and possibly a different gateway.

### 2. Add the host to the inventory
In `ansible/inventory/hosts.ini`, under `[control_nodes]`, replace the commented line:
```ini
lxc-ansible     ansible_host=10.0.40.<IP>   vmid=<VMID>
```
- **Check:** `ansible/.venv/bin/ansible-inventory --graph` shows `lxc-ansible` under `control_nodes`, and not under `guests`.
- Commit the line, so the clone on the LXC has the same inventory.

### 3. Firewall rules (inter-VLAN)
- **Control LXC →**
  - `pve`: 22 and 8006
  - all 10.0.40.x hosts: 22
  - github.com: 22 and 443
  - discord.com: 443
  - Debian mirrors: 80/443
- **Laptop →** the control LXC: 22
- **Check after step 4:** from the LXC, `ssh root@10.0.10.10 true` works and `curl -sI https://discord.com` answers.

### 4. Provision the LXC
```sh
task ansible:control:provision
```
- **Expected:** the template download and the LXC creation report `changed`, the container starts, and "Wait for SSH" succeeds within 5 minutes.
- **Likely failures:**
  - **Template name not found:** run `pveam update && pveam available | grep debian-13` on `pve` and pass `-e control_node_lxc_template=<name>`.
  - **403 from the API:** missing token privileges, see the prerequisites.
  - **SSH timeout:** wrong VLAN tag, gateway or firewall.
- **Check:** `ssh root@10.0.40.<IP>` works with my laptop key.

### 5. Configure it
```sh
task ansible:control:apply
```
(Don't `--limit` this run: `ssh_access` has to reach every host to authorize the key.)
- The run **pauses and prints a deploy key**. Add it on GitHub under repo → Settings → Deploy keys, **read-only**, then press Enter.
- **Expected:**
  - The key is authorized on `pve`, `vm-docker`, `lxc-tailscale` and `lxc-pbs`.
  - The repo is cloned to `/opt/homelab`, and both vault files are copied.
  - Both timers are enabled.
- **Likely failures:**
  - **The go-task `.deb` URL returns 404:** check the release asset name on GitHub and override `control_node_task_version`.
  - **The git clone fails:** the deploy key isn't added yet, or github.com:22 is blocked.

### 6. Verify on the LXC
```sh
ssh root@10.0.40.<IP>
systemctl list-timers 'homelab-*'      # both timers with next run times
cd /opt/homelab && git status && git log -1
task ansible:proxmox:ping              # the control node reaches every host
```

### 7. First runs, by hand, with me watching
```sh
systemctl start homelab-deploy.service && journalctl -fu homelab-deploy.service
```
- **Expected:** the first run always deploys, because there's no state file yet. It should finish healthy and write `/var/lib/homelab/deployed-sha`. A second run should print "already deployed".
- **Precondition:** the image pins are confirmed against what's running. Above all, the TimescaleDB digest must match the image on the VM, and the observability stack in the repo (logporter, no cAdvisor) must match the VM. Otherwise this deploy changes things.

```sh
systemctl start homelab-maintenance.service && journalctl -fu homelab-maintenance.service
```
- **Expected:** the Discord summary arrives and every host shows `ok`. If the PVE kernel was updated, a "reboot pending" warning arrives as well.
- **Test the failure alert:** trigger a failing run and check that a Discord message with journal lines arrives, for example:
  ```sh
  systemd-run --unit=fail-test -p OnFailure=homelab-notify@%n.service false
  ```

### 8. Afterwards
- My laptop is no longer needed for scheduled runs. Manual runs still work from either place.
- **Vault changes:** after editing the vault on my laptop, run `control:apply` again to copy it over. The vault isn't in git.
- **Watch for drift:** I shouldn't edit stacks in Dockge any more. The control node's deploy overwrites those edits and doesn't pull them back.
- **Pause automation:** `systemctl disable --now homelab-maintenance.timer homelab-deploy.timer` on the LXC.

## Open assumptions to verify along the way

These were written without access to the real hosts:
- the LXC options: template name, `vmbr0` with tag 40, gateway 10.0.40.1, `local-lvm`
- the go-task 3.54.0 `.deb` URL
- the timezone (Europe/Berlin)
- the `community.proxmox.proxmox` module parameters

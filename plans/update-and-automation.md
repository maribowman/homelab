# Homelab: automated snapshots, backups & updates

## Context

The repo already has Ansible + Taskfile for snapshot → apt upgrade → reboot, but everything is run by hand from the laptop. Gaps:
- **Stale IPs.** The network moved to VLANs (10.0.x.x), but the repo still uses the 192.168.0.x addresses, so nothing can reach the hosts.
- The PVE host and the PBS LXC are not in the inventory, so they never get updated.
- The backup setup (PBS → Synology, PVE vzdump job) exists only as manual steps in `proxmox-backup-server-setup.md`.
- Docker images all use `:latest`, are never pulled, and nothing checks their versions.
- Nothing runs on a schedule and nothing reports results.
- Bugs:
  - No FQCN on `apt`/`stat`/`reboot` (`ansible/roles/update_proxmox_machines/tasks/main.yaml`).
  - The STACK enum says `ha-timescaledb`, but the directory is `timescaledb` (`ansible/taskfiles/docker.yaml`).
  - The rsync pull-back copies remote `data/` dirs (including the DB) to the laptop (`ansible/roles/sync_docker_stacks/tasks/main.yaml`).
  - Snapshot names use only the date, so they collide when run twice a day.
  - `retention_days` is never used.

**Goal:** no manual updates any more. Backups run daily, updates weekly with a snapshot first, image bumps come in as Renovate PRs, and every run reports to Discord.

**Decisions (confirmed with user):**
- Scheduler: control LXC with systemd timers.
- Images: pinned tags + Renovate.
- Guests and PBS update automatically. The PVE host gets packages upgraded, is never rebooted automatically, and sends a notification instead.
- Notifications: Discord webhook.
- Out of scope: TrueNAS (standalone hardware) and both Home Assistant instances.

**Is Ansible the right tool?** Yes, keep it.
- Ansible **configures** the native tools that do the heavy lifting: PVE backup jobs, PBS prune/GC/verify, PVE/PBS webhooks.
- Ansible **orchestrates** the update order: snapshot → update → health check → notify.
- Later, optional: OpenTofu (`bpg/proxmox`) for declarative VM/LXC provisioning, and committing an encrypted `vault.yaml`.
- Dockge UI edits drift from git. Git is the source of truth from now on.

## Target schedule

| When | What | Driver |
|---|---|---|
| Daily 01:00 | vzdump of all guests except PBS (1001) → PBS → Synology | native PVE job (configured by A) |
| Daily (PBS) | prune (3 last / 3 weekly / 3 monthly), GC, verify | native PBS jobs (configured by A) |
| Daily 05:00 | `git pull`; if HEAD changed → docker deploy (pull + `up -d`) | `homelab-deploy.timer` (D) |
| Sun 04:00 | preflight → snapshot → update guests one at a time → health check → PBS → PVE host (no reboot) → Discord summary | `homelab-maintenance.timer` (D) |
| Continuous | Renovate PRs; automerge patch/minor when CI is green | GitHub (C + E) |

---

## Execution model for parallel subagents

```
Wave 0 (lead, sequential) ──► Wave 1: A  B  C  D  E  (5 subagents in parallel, one worktree each) ──► Wave 2 (lead: merge, integrate, live-verify)
```

**Rules for every Wave 1 agent:**
1. Run as `general-purpose` with `isolation: "worktree"`, branched from the Wave 0 commit. Commit to your own branch `feat/<letter>-<slug>` and do not merge.
2. **Edit only the files listed under "Owns".** If you need a change in a file you don't own, write it in your final report under "Requests to lead" and don't make it.
3. Use the names in **Shared contracts** exactly. Don't invent new group names, var names or vault keys. If you need a new var, put it in your role's `defaults/main.yaml`, prefixed with your role name.
4. **Allowed against real hosts:** read-only commands (`ssh … cat/ls/pvesh get/docker inspect`), `--syntax-check`, and `--check --diff`. **Not allowed:** anything that changes real hosts. The lead does all live runs in Wave 2.
5. Before finishing: `ansible-playbook --syntax-check` on every playbook you touched, and `ansible-lint` on your roles, both via `ansible/.venv`.
6. Final report: files changed, how you verified them, open questions, and requests to lead.

### Shared contracts (fixed in Wave 0; all agents depend on these)

**Inventory groups** (`ansible/inventory/hosts.ini`):

| Group | Hosts |
|---|---|
| `proxmox_hosts` | `pve` 10.0.10.10 |
| `pbs_hosts` | `lxc-pbs` 10.0.40.20, vmid 1001 |
| `ubuntu_nodes` | `vm-docker` 10.0.40.40, vmid 101 |
| `debian_nodes` | `lxc-tailscale` 10.0.40.90, vmid 1000 |
| `docker_hosts` | `vm-docker` |
| `control_nodes` | `lxc-ansible` (IP/vmid TBD by user) |
| `guests` (parent) | children: `ubuntu_nodes`, `debian_nodes`, `pbs_hosts` |

Every host keeps a `vmid` hostvar, except `pve`.

**Non-secret vars** (`ansible/inventory/group_vars/all/vars.yaml`):
- Existing (`pve_host` changes to 10.0.10.10): `pve_host`, `pve_node`, `pve_user`, `pve_token_id`, `dockge_stacks_path`.
- PBS and NAS: `pbs_host: 10.0.40.20`, `pbs_datastore: synology-backup`, `pve_pbs_storage_id: pbs-synology`, `nas_host: 10.0.40.10`, `nas_share: backups`, `nas_subdir: proxmox_backup_home`.
- Backup schedule and retention: `backup_schedule: "01:00"`, `backup_exclude_vmids: [1001]`, `pbs_keep_last: 3`, `pbs_keep_weekly: 3`, `pbs_keep_monthly: 3`.
- Snapshots: `snapshot_prefix: pre_update`, `snapshot_retention: 2`.
- Timers: `maintenance_oncalendar: "Sun *-*-* 04:00"`, `deploy_oncalendar: "*-*-* 05:00"`.
- Control node: `repo_url: git@github.com:maribowman/homelab.git`, `control_repo_path: /opt/homelab`.
- Remove `retention_days`.

**Vault keys** (`vault.yaml`, documented in the header of `vars.yaml`): `pve_token_secret` (existing), `pbs_user_password` (for `proxmox@pbs`), `synology_cifs_password`, `discord_webhook_url`.

**`notify_discord` role interface** (stub in Wave 0, real version from E):
```yaml
- ansible.builtin.include_role:
    name: notify_discord
  vars:
    notify_title: "Maintenance: vm-docker"
    notify_message: "Upgraded 12 packages, rebooted"
    notify_level: info   # info | warn | error
```
- Always runs on localhost and never fails the play.
- Anyone else (shell, systemd) calls the webhook directly with curl.

**`deploy_docker_stacks` interface** (C owns it, B consumes it):
- `docker_state` takes `up`, `down`, `restart` (keep their current behaviour), plus new `pull` (pull, then `up -d --remove-orphans`) and `health` (fail if any container isn't running or healthy after a timeout).
- `target_stack` stays the same.

**Taskfile passthrough:** every task that calls a playbook appends `{{.CLI_ARGS}}`, so `task ansible:proxmox:update -- --limit lxc-tailscale` and `task ansible:docker:deploy -- -e sync_pull_remote=false` work.

**Taskfile wiring:** Wave 0 adds empty `ansible/taskfiles/backup.yaml` and `ansible/taskfiles/control.yaml` and includes them in `ansible/Taskfile.yaml` with `PLAYBOOK` vars pointing at `playbooks/backup.yaml` and `playbooks/control.yaml`. Agents fill their own taskfile and never touch `ansible/Taskfile.yaml`.

---

### Wave 0: Foundation (lead, sequential, ~1 commit)

1. Commit this plan (`plans/update-and-automation.md`) together with the Wave 0 changes.
2. **IP renumbering:**

   | Host | Old | New |
   |---|---|---|
   | Proxmox `pve` | 192.168.0.100 | 10.0.10.10 |
   | PBS | 192.168.0.101 | 10.0.40.20 |
   | `vm-docker` / Dockge / Grafana / Alloy | 192.168.0.69 | 10.0.40.40 |
   | `lxc-tailscale` | 192.168.0.155 | 10.0.40.90 |
   | Synology Diskstation | 192.168.0.3 | 10.0.40.10 |
   | Home Assistant (legacy) | 192.168.0.2 | 10.0.40.30 |
   | TrueNAS | 192.168.0.139 | 10.0.40.100 |

   - Files to change: `hosts.ini`, `vars.yaml`, `docker/stacks/observability/main.alloy:31`, every URL in `homer/config.yml`, `proxmox-backup-server-setup.md:124`, `AGENTS.md`. Find them with `git grep 192.168.0`.
   - Run `ssh root@10.0.10.10 pvesm status` and check `/etc/pve/storage.cfg` for the PBS storage. If it still uses the old IP, backups are failing right now. Tell the user; A fixes it as code.
   - Check that the PBS vmid is still 1001 (`pct list`).
   - The future HAOS VM (10.0.10.198) stays out of the inventory. The vzdump job ("all except 1001") picks it up on its own once it exists.
3. Write the **Shared contracts** into the inventory and vars: the groups, the vars, the vault-key comment, and the taskfile stubs and includes.
4. Stub `ansible/roles/notify_discord/`: defaults plus one `ansible.builtin.debug` task.
5. Bug fixes in files nobody else owns:
   - Remove `retention_days`.
   - Make `CLAUDE.md` a one-line `@AGENTS.md` import, since it is currently a duplicate.
   - The other bugs are fixed by their owners: B (FQCN), C (STACK enum, rsync).
6. Gate: `task ansible:proxmox:ping` passes against the new IPs, and `--syntax-check` still passes. Commit to `main`.
7. Start A–E in **one message** (5 parallel Agent calls, `isolation: "worktree"`). Each prompt points at `plans/update-and-automation.md`, names its workstream letter, and repeats the rules.

### Wave 1: Subagent briefs

#### A — Backups as code
- **Goal:** the existing manual PBS/PVE backup setup becomes idempotent Ansible.
- **Owns:** `ansible/roles/pbs_server/`, `ansible/roles/pve_backup_jobs/`, `ansible/roles/qemu_guest_agent/`, `ansible/playbooks/backup.yaml`, `ansible/taskfiles/backup.yaml`, `proxmox-backup-server-setup.md`.
- **Tasks:**
  - `pbs_server` on `pbs_hosts`:
    - `cifs-utils`, `/etc/pbs-synology-credentials` (0600, from vault), and an `ansible.posix.mount` of `//{{nas_host}}/{{nas_share}}` with the existing options.
    - Datastore, prune job, GC, verify job and the webhook notification target to `discord_webhook_url`, via `proxmox-backup-manager`.
    - Idempotent: read `… list --output-format json` first, then create or update only on a diff.
  - `pve_backup_jobs` on `proxmox_hosts`:
    - PBS storage `pve_pbs_storage_id` with the fingerprint from PBS (`proxmox-backup-manager cert info`, delegated).
    - vzdump job: snapshot mode, zstd, `exclude: backup_exclude_vmids`, `schedule: backup_schedule`.
    - Webhook notification target and matcher for backup failures.
    - Use `pvesh` with JSON idempotency checks, or a `community.proxmox` module if one covers it (check the installed collection docs).
  - `qemu_guest_agent` on `ubuntu_nodes`: install and start the agent. Report "set `agent: 1` on vmid 101" as a request to lead (VM config change and reboot).
  - Taskfile: `backup:apply`, `backup:check` (`--check --diff`), `backup:syntax`.
  - Doc: keep only the manual steps (PBS install script, Synology user and permissions) and link to the roles.
- **Done when:** syntax-check and lint are clean, and `--check --diff` against the real hosts shows only the expected changes (or none, where the config already matches).

#### B — OS updates, snapshots, rollback
- **Goal:** a safe weekly update run of guests, PBS and the PVE host.
- **Owns:** `ansible/roles/snapshot_proxmox_machines/`, `ansible/roles/update_proxmox_machines/`, `ansible/roles/update_pve_host/`, `ansible/roles/preflight_backup_check/`, `ansible/playbooks/proxmox.yaml`, `ansible/playbooks/rollback.yaml`, `ansible/taskfiles/proxmox.yaml`.
- **Tasks:**
  - Preflight (localhost, PVE API):
    - Look up the newest vzdump task per `vmid`.
    - If it is older than 36h or failed, send `warn` but continue. Treat a missing backup job the same way, since A may not be live yet.
  - Snapshot:
    - Name: `{{snapshot_prefix}}_<YYYYMMDD_HHMM>`, `retention: snapshot_retention`.
    - Wrap the per-host flow in block/rescue: if the snapshot fails, skip the host and send `error`.
  - Guest play (`hosts: guests`, `serial: 1`, FQCN):
    - Docker hosts: `deploy_docker_stacks docker_state=down`.
    - apt dist-upgrade and autoremove.
    - Reboot if `/var/run/reboot-required` exists.
    - Docker hosts: `docker_state=up`, then `health`.
    - Send `info` with the package count; on rescue send `error` with the rollback command.
  - `update_pve_host` (`hosts: proxmox_hosts`):
    - Assert that the no-subscription (or enterprise) repo is configured.
    - `apt dist-upgrade`. **Never reboot.**
    - Reboot needed (`uname -r` ≠ newest `/boot/vmlinuz-*`, or `needrestart -b` says so) → send `warn` "PVE host reboot pending".
  - Play order: preflight → guests (PBS last among guests) → PVE host.
  - `rollback.yaml`: `-e target=<host>`, find the newest `{{snapshot_prefix}}_*` snapshot and roll back via `community.proxmox.proxmox_snap state=rollback`.
  - Tasks `proxmox:rollback HOST=…` and `proxmox:check`. Add `{{.CLI_ARGS}}` to every task.
- **Done when:** syntax-check and lint are clean, and `--check` of `proxmox.yaml --limit lxc-tailscale` runs through. Note that reboot and snapshot get skipped in check mode.

#### C — Container images & deploy
- **Goal:** pinned, Renovate-managed images and a deploy that pulls and runs a health check.
- **Owns:** `docker/stacks/*/compose.yaml`, `docker/dockge/compose.yaml`, `renovate.json`, `ansible/roles/deploy_docker_stacks/`, `ansible/roles/sync_docker_stacks/`, `ansible/taskfiles/docker.yaml`.
- **Tasks:**
  - Pin every `:latest` to the version that is running now (read-only: `ssh root@10.0.40.40 docker inspect …` / image labels).
    - Own image `ghcr.io/maribowman/roastbeef-swag`: `latest@sha256:…`.
    - Leave `timescale/timescaledb-ha:pg18` as is, but add a digest.
  - `renovate.json`:
    - `docker-compose` manager.
    - Group `grafana/*` and `prom/*`.
    - Automerge minor/patch/digest.
    - `major` and `timescaledb-ha` never automerge and get a `needs-review` label.
    - Schedule "every weekend", `pinDigests: true`.
  - `deploy_docker_stacks`:
    - Add the `pull` and `health` states (contract above).
    - Replace `ignore_errors: true` with collected failures → one `notify_discord` `error` → fail at the end.
  - `sync_docker_stacks`:
    - Add `--exclude=data` to the pull-back.
    - Gate the pull-back with `sync_pull_remote` (default `true`).
  - Taskfile:
    - Fix the STACK enum (`timescaledb`).
    - `deploy` uses `docker_state=pull`.
    - Add `{{.CLI_ARGS}}` to every task.
- **Done when:** `docker compose -f … config -q` passes for every stack, `npx --yes renovate-config-validator` passes, and syntax-check and lint are clean.

#### D — Control node & schedulers
- **Goal:** an always-on LXC that runs the playbooks on timers.
- **Owns:** `ansible/roles/control_node/`, `ansible/roles/ssh_access/`, `ansible/playbooks/control.yaml`, `ansible/taskfiles/control.yaml`.
- **Tasks:**
  - Play 1 (localhost): create the Debian 13 LXC via `community.proxmox.proxmox` (vmid and IP from the inventory, unprivileged, 1 vCPU / 1 GB / 8 GB, `onboot: true`).
  - `control_node`:
    - Packages: git, python3-venv, rsync, curl, go-task (from the GitHub release).
    - ed25519 deploy key → print the public key and pause for the user to add it to GitHub.
    - `git clone repo_url control_repo_path`.
    - Copy `vault.yaml` and `.vault.pass` from the laptop (0600).
    - unattended-upgrades with `Automatic-Reboot "true"` at 06:00, for the control node itself.
  - `ssh_access`: generate the control node's SSH key for root and use `ansible.posix.authorized_key` on all other hosts.
  - systemd templates (in the role's `templates/`):
    - `homelab-maintenance.{service,timer}`: `flock /run/homelab.lock sh -c 'git pull --ff-only && task ansible:proxmox:update'`, `OnCalendar={{maintenance_oncalendar}}`.
    - `homelab-deploy.{service,timer}`: pull. If HEAD changed, run `task ansible:docker:deploy -- -e sync_pull_remote=false`. `OnCalendar={{deploy_oncalendar}}`.
    - Both timers: `Persistent=true`, `OnFailure=homelab-notify@%n.service`.
    - `homelab-notify@.service`: curl `discord_webhook_url` (from a 0600 env file) with the last 30 `journalctl -u %i` lines.
  - Tasks: `control:provision` and `control:apply`.
- **Done when:** syntax-check and lint are clean, and `systemd-analyze verify` passes on the rendered units (render them locally with `ansible -m template` into the scratchpad).

#### E — Notifications & CI
- **Goal:** a real Discord notifier and a CI gate for Renovate automerge.
- **Owns:** `ansible/roles/notify_discord/`, `.github/workflows/ci.yaml`, `.ansible-lint`, `.yamllint`.
- **Tasks:**
  - `notify_discord`:
    - `ansible.builtin.uri` POST of an embed: colour by level (info green, warn amber, error red); hostname, playbook and timestamp in the footer.
    - `delegate_to: localhost`, `become: false`, `failed_when: false`, `no_log: true`.
    - `notify_run_once` default `false`.
    - Skip with a debug message when `discord_webhook_url` is undefined (CI, laptop without vault).
  - CI (pull_request and push to main):
    - yamllint, ansible-lint (profile `production`, temporarily relaxed to `moderate` if needed).
    - `ansible-galaxy collection install -r ansible/requirements.yaml`.
    - `--syntax-check` for every `ansible/playbooks/*.yaml`, with a dummy vault password and an empty stub vault created in CI.
    - `docker compose config -q` per stack.
  - The lint configs must exclude `docker/stacks/*/data` and `ansible/.venv`.
- **Done when:** the workflow passes locally via `act`, or every step passes when run by hand, and the role works with and without a webhook URL.

### Wave 2: Integration & live rollout (lead)

1. Merge order: **E → C → B → A → D**. Rebase each branch, run lint and syntax-check after each merge, and handle "Requests to lead" along the way.
2. Add the end-of-run Discord summary to `playbooks/proxmox.yaml`: per-host results via hostvars, then `notify_discord` with `run_once`.
3. Update the docs: `README.md` and `AGENTS.md` (roles table, tasks, schedule table, vault keys, manual steps).
4. Live rollout, with the user present:
   1. `backup:apply`
   2. Manually trigger vzdump
   3. `proxmox:update -- --limit lxc-tailscale`
   4. Full `proxmox:update`
   5. `docker:deploy`
   6. `control:provision` + `control:apply`
   7. Start both services once by hand

## Manual steps for the user (cannot be automated)
- Create the Discord webhook and add the vault keys (`ansible-vault edit ansible/inventory/group_vars/all/vault.yaml`).
- Give the PVE API token `maintenance` the privileges VM.Snapshot, VM.Snapshot.Rollback, VM.Audit, VM.Allocate (to create the control LXC), Sys.Audit, Datastore.Audit, Datastore.AllocateSpace.
- Pick the control LXC IP (10.0.40.x) and vmid.
- Install the Renovate GitHub App on `maribowman/homelab` and add a branch-protection rule on `main` that requires CI.
- Add the deploy key printed by D to GitHub (read-only).
- Inter-VLAN firewall rules:
  - control LXC → `pve` 10.0.10.10 (22, 8006) and all 10.0.40.x hosts (22)
  - `pve` → PBS (8007)
  - PBS → Diskstation (445)
  - control LXC → internet (443) for GitHub and Discord

## Verification
0. After Wave 0, `task ansible:proxmox:ping` passes for every host, the Alloy NAS target is `up` in Prometheus, and the Homer links work.
1. CI is green on every agent branch and on `main` after each merge.
2. `backup:check` shows the expected diff. After `backup:apply`, running it again reports `changed=0`.
3. Backups:
   - A vzdump triggered by hand lands in PBS and verify passes.
   - A deliberate failure (bogus storage) fires the PVE Discord webhook.
   - Restore one backup to a spare vmid, then delete it.
4. Updates:
   - `proxmox:update -- --limit lxc-tailscale`: snapshot is created, packages upgraded, health check green, Discord `info` arrives.
   - `proxmox:rollback HOST=lxc-tailscale` restores the snapshot.
   - The PVE host run sends the "reboot pending" warning when it applies.
5. Images: a Renovate PR appears and CI runs. After merge, `systemctl start homelab-deploy.service` redeploys and `docker ps` shows the new tag.
6. Scheduler:
   - `systemctl list-timers` on the control LXC shows both timers.
   - `systemctl start homelab-maintenance.service` runs the full flow.
   - A forced failure (e.g. a bad `--limit`) fires the `OnFailure` Discord message.

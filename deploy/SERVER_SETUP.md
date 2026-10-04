# Server setup — Ubuntu VPS in Turkey

Copy-paste steps to take a fresh Ubuntu VPS to "the Tabdeal recorder runs as a managed
service, survives reboots, and Parham cannot be locked out." Written for someone who has
never administered a Linux server before. Follow the sections **in order** — the ordering
is deliberate (see "Why this order" boxes) and skipping ahead is how people lock themselves
out of their own server.

Conventions used below:

- Lines starting with `$` are commands to run **on the server**, over SSH, as a normal step.
- Lines starting with `C:\>` are commands to run **on Parham's Windows laptop**, in
  PowerShell.
- A step marked **ASK PARHAM BEFORE RUNNING** changes something that can affect whether you
  can reach the server at all (SSH access or the firewall). Do not run it unattended, and do
  not run it until every check right before it has passed.
- Every command below was written against documented Ubuntu 22.04 ("jammy") / 24.04
  ("noble") behaviour, but **could not be executed on a real Ubuntu server from this
  Windows development machine** — there is no Docker/WSL/Linux VM available here. Treat
  each command as "should be correct, not yet proven on your server." If anything errors,
  stop, copy the exact error, and do not improvise past it.
- Deployment layout assumed throughout: a dedicated non-root user **`tbot`**, repo cloned to
  **`/opt/tbot/trade-bot`**. If you use a different user or path, you must also edit
  `deploy/systemd/tbot-recorder.service` (the `User=` and path lines) to match.

---

## 0. Check the Ubuntu version

```
$ lsb_release -a
```

You should see `Ubuntu 22.04.x LTS (jammy)` or `Ubuntu 24.04.x LTS (noble)`. Both are
covered below; every place a command differs between the two is called out explicitly.
Everything else is identical on both.

**Ubuntu 26.04 ("resolute")** — the real Turkey VPS runs this. Verified there on 2026-10-04:
Docker's official apt repo has a `resolute` suite (section 7.3 works unchanged), the uv
installer works, chrony is preinstalled, and `ssh.socket` is active — so sshd is
socket-activated exactly as described for 24.04 in section 5.

---

## 1. Reconnaissance — find out what's actually running (read-only, safe)

Before touching SSH or the firewall you need to know, on **this specific server**: which
port SSH is really listening on (VPS providers sometimes change it from 22), and what else
is listening that a firewall could accidentally cut off (a dashboard, a provider's monitoring
agent, etc.).

```
$ sudo ss -tlnp
```

Read the output column by column: `Local Address:Port` tells you the port, `Process` (last
column) tells you what's listening. Write down:

- The port `sshd` (or `ssh`) is listening on. This is almost always `22`, but **do not
  assume** — if your provider configured something else, using `22` in the firewall step
  below will lock you out the moment you reconnect.
- Every other listening service and its port (e.g. a provider's agent, a web panel). **Any
  one of these will stop being reachable from the internet once `ufw` is enabled** (step 6),
  unless you explicitly `ufw allow` it first. If you don't recognize something, ask Parham /
  the VPS provider before deciding to block it — don't guess.

> **Why this order:** you cannot safely write a firewall rule for "allow SSH" without first
> knowing which port SSH is actually on, on this server.

---

## 2. Timezone and clock sync

Tabdeal's HMAC request signing depends on the client clock (CLAUDE.md section 6 / `exchange.
recv_window_ms` in `config/default.yaml`); a clock more than a few seconds off causes every
signed request to be rejected. Fix this early, before anything else needs a working clock.

```
$ sudo timedatectl set-timezone UTC
$ timedatectl
```

Confirm the output shows `Time zone: UTC (UTC, +0000)`.

Most Ubuntu installs — cloud or otherwise — already have a running NTP client, either
`systemd-timesyncd` (the default) or `chrony` (common on some cloud provider images,
and it overrides timesyncd when installed). Find out which one is active:

```
$ timedatectl
```

Look for the line `NTP service: active`. Then verify the actual offset:

**If `chrony` is installed** (`systemctl status chrony` shows it running):

```
$ chronyc tracking
```

Look at the `System time` line, e.g. `System time : 0.000412345 seconds fast of NTP time` —
the number must be **well under 1 second** (ideally under 50ms). If `chronyc` is not found,
you don't have chrony; use the timesyncd check below instead.

**If `systemd-timesyncd` is active** (the Ubuntu default when chrony isn't installed):

```
$ timedatectl timesync-status
```

Look for the `Offset` line, e.g. `Offset: -3.212ms` — again, must be well under 1 second.
If this subcommand doesn't exist on your Ubuntu version, use `timedatectl show-timesync
--all | grep -i offset` instead.

If neither tool reports a service, install and enable one (Ubuntu 22.04 and 24.04: identical
command):

```
$ sudo apt update
$ sudo apt install -y chrony
$ sudo systemctl enable --now chrony
$ chronyc tracking
```

**Do not proceed past this section until you've seen an actual offset number under 1
second.** This is not optional — re-read CLAUDE.md section 3.6: every Tabdeal signed request
depends on it.

---

## 3. Create a non-root sudo user

You are presumably logged in as `root` (or a provider-created admin user) right now. Create
a dedicated, least-privilege user for everything that follows. This step is purely additive —
it does not touch your existing login, so it cannot lock you out by itself.

```
$ sudo adduser tbot
```

(You'll be prompted for a password — set one; it won't be used for SSH login once we switch
to keys, but `sudo` still needs it. Blank through the "Full Name" etc. prompts if you like.)

```
$ sudo usermod -aG sudo tbot
$ sudo usermod -aG docker tbot        # harmless if the docker group doesn't exist yet; re-run after section 5
```

---

## 4. Set up key-based login for `tbot` — and TEST it before anything else changes

### 4.1 Generate (or reuse) an SSH key pair on Parham's Windows laptop

If you don't already have one:

```
C:\> ssh-keygen -t ed25519 -C "parham-trade-bot-vps"
```

Accept the default path (`C:\Users\<you>\.ssh\id_ed25519`) or choose a dedicated one; press
Enter through the passphrase prompt or set one (recommended).

### 4.2 Copy the public key to the server, for the `tbot` user

From **Parham's Windows laptop** (Windows 10/11 ship an OpenSSH client that includes `scp`):

```
C:\> type $env:USERPROFILE\.ssh\id_ed25519.pub
```

Copy that single line of text. Then, back in your **existing** server session (still logged
in as root / the original admin user — do not log out):

```
$ sudo -u tbot mkdir -p /home/tbot/.ssh
$ sudo -u tbot nano /home/tbot/.ssh/authorized_keys
```

Paste the public key as the only line, save (`Ctrl+O`, Enter, `Ctrl+X` in `nano`), then fix
permissions (SSH refuses keys with overly permissive files):

```
$ sudo chmod 700 /home/tbot/.ssh
$ sudo chmod 600 /home/tbot/.ssh/authorized_keys
$ sudo chown -R tbot:tbot /home/tbot/.ssh
```

### 4.3 Test the new login in a SECOND session — keep this first session open

Open a **brand-new** PowerShell window on the Windows laptop (do **not** close the session
you're already using) and run:

```
C:\> ssh tbot@<server-ip>
```

using the real SSH port you found in section 1 if it isn't 22:

```
C:\> ssh -p <port> tbot@<server-ip>
```

You should land in a shell as `tbot` with no password prompt (only a passphrase prompt if you
set one on the key). Then confirm `sudo` works:

```
$ sudo whoami
```

should print `root`. **Only once this second session works** should you continue. If it
fails for any reason, fix it with the first (still-open) session — do not touch `sshd_config`
or the firewall yet.

> **Why this order:** disabling password login (next section) is irreversible without
> already having a working key-based session. Proving that session works *first*, in
> parallel with the original session staying open, means there are always two independent
> ways in while you make the risky change.

---

## 5. Harden SSH — **ASK PARHAM BEFORE RUNNING**

Only do this after section 4.3 has actually succeeded, and keep **both** the original session
and the new `tbot` session open while you do it.

```
$ sudo nano /etc/ssh/sshd_config
```

Confirm (or set) these lines — note the port must be the **real** port from section 1, which
may already be correct if it's 22 and your provider never changed it:

```
Port <real-ssh-port>
PasswordAuthentication no
PermitRootLogin prohibit-password
```

> **Drop-in files override `sshd_config` (verified on the real server, Ubuntu 26.04).**
> `sshd_config` starts with `Include /etc/ssh/sshd_config.d/*.conf`, and for each keyword sshd
> keeps the **first** value it reads. Cloud images ship
> `/etc/ssh/sshd_config.d/50-cloud-init.conf` with `PasswordAuthentication yes`, which wins
> over anything you write further down in `sshd_config` — editing `sshd_config` alone leaves
> password login **on**. Put the hardening in its own early drop-in instead:
> ```
> $ printf 'PasswordAuthentication no\nPermitRootLogin prohibit-password\nKbdInteractiveAuthentication no\n' | sudo tee /etc/ssh/sshd_config.d/00-tbot-hardening.conf
> ```
> and always check the **effective** values, not the file you edited:
> ```
> $ sudo sshd -T | grep -Ei '^(port|passwordauthentication|permitrootlogin|kbdinteractiveauthentication) '
> ```

Before restarting the SSH daemon, validate the config file so a typo can't break it:

```
$ sudo sshd -t
```

If that prints nothing, it's valid. Then restart:

```
$ sudo systemctl restart ssh
```

> **Ubuntu 24.04 ("noble") note — read this if you changed `Port`:** on 24.04, `sshd` is
> **socket-activated** by default: the thing actually listening on a port is
> `ssh.socket`, not `ssh.service`, and `ssh.socket`'s own `ListenStream=` is what decides
> the port — `sshd_config`'s `Port` line is not consulted until a connection has already
> arrived on a socket `ssh.socket` is listening on. `sudo systemctl restart ssh` only
> restarts `ssh.service`, so changing `Port` and restarting `ssh` on 24.04 **does not
> move the listener** — the daemon keeps listening on whatever `ssh.socket` was already
> bound to (usually 22), silently ignoring your new `Port` value. This fails safe (you
> will not get locked out by this alone — the old port keeps working), but it means: (a)
> `ssh -p <new-port> ...` will not connect until you also fix the socket unit, and (b) if
> you write a `ufw allow <new-port>/tcp` rule in section 6 believing the daemon moved,
> you've opened a port nothing is listening on while the real port is still 22 — a
> confusing, wrong firewall rule, not a lockout. On 24.04, either leave `Port` at its
> current value, or change it correctly with:
> ```
> $ sudo systemctl edit ssh.socket
> ```
> and set `ListenStream=<real-ssh-port>` under `[Socket]`, then
> `sudo systemctl restart ssh.socket`. Confirm which one actually owns the listening
> socket either way with `sudo ss -tlnp | grep ssh` before trusting any port number you
> put in a firewall rule. 22.04 ("jammy") does not socket-activate `sshd` by default, so
> `Port` + `systemctl restart ssh` behaves as the section above assumes there.

**Do not close any existing session yet.** Open a **third**, brand-new terminal and confirm
key-based login as `tbot` still works exactly as in 4.3. Only once that third test succeeds
should you consider this step done. If it fails, use one of the still-open sessions to revert
`/etc/ssh/sshd_config` and `sudo systemctl restart ssh` again.

---

## 6. Enable the firewall — **ASK PARHAM BEFORE RUNNING**

Only after section 5's third-session test has succeeded.

```
$ sudo apt install -y ufw
$ sudo ufw allow <real-ssh-port>/tcp
```

If section 1 found other services you decided you need reachable from the internet (e.g. a
provider monitoring agent), allow those too, explicitly, now:

```
$ sudo ufw allow <other-port>/tcp
```

Anything you don't explicitly allow here will become unreachable from outside the server the
moment you enable ufw — that is the point, but it means **this is your last chance** to add a
rule for anything from section 1's list that you actually need.

```
$ sudo ufw default deny incoming
$ sudo ufw default allow outgoing
$ sudo ufw enable
```

Confirm:

```
$ sudo ufw status verbose
```

You should see your SSH port (and nothing else you didn't explicitly allow) listed as
`ALLOW`.

> **Docker-published ports bypass ufw.** Docker writes its own iptables rules for any
> `ports:` mapping, ahead of ufw's chains, so a container port published as `8080:8080` is
> reachable from the internet even though `ufw status` does not list it. Today
> `deploy/docker-compose.yml` publishes **no** ports (the recorder only makes outbound
> calls), so nothing is exposed. Any future dashboard or metrics endpoint **must** bind to
> loopback only — `ports: ["127.0.0.1:8080:8080"]` — and be reached through an SSH tunnel
> (`ssh -L 8080:127.0.0.1:8080 tbot@<server-ip>`), never published on `0.0.0.0`. Test a **fourth**, brand-new SSH session now, exactly as in 4.3/5. If it works, the
server is hardened and still reachable.

---

## 7. Install git, uv, and Docker

### 7.1 git

Identical on both Ubuntu versions:

```
$ sudo apt update
$ sudo apt install -y git
```

### 7.2 uv

Identical on both Ubuntu versions (installs to `~/.local/bin` for the current user — run this
as `tbot`, not root):

```
$ curl -LsSf https://astral.sh/uv/install.sh | sh
$ source $HOME/.cargo/env 2>/dev/null || source $HOME/.local/bin/env
$ uv --version
```

If `uv --version` doesn't work in a fresh shell afterwards, log out and back in (`exit` then
reconnect) so your updated `PATH` takes effect, and try again.

### 7.3 Docker Engine + the Compose plugin

The official Docker install steps are identical for 22.04 and 24.04 — the one line that
differs (the Ubuntu codename, `jammy` vs `noble`) is picked up automatically from
`/etc/os-release` rather than hardcoded, so the same commands work on both:

```
$ sudo apt update
$ sudo apt install -y ca-certificates curl
$ sudo install -m 0755 -d /etc/apt/keyrings
$ sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
$ sudo chmod a+r /etc/apt/keyrings/docker.asc
$ echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
$ sudo apt update
$ sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Add `tbot` to the `docker` group (this is the re-run from section 3, now that the group
exists), then start a **new** SSH session so group membership takes effect — group changes
never apply to an already-open session.

> **The `docker` group is root-equivalent.** Any member can run
> `docker run -v /:/host ...` and read or change every file on the server, `.env` and SSH keys
> included, without `sudo`. Add only `tbot` to it, treat the `tbot` SSH key with the same care
> as a root key, and never add a service account or a second person to this group.

```
$ sudo usermod -aG docker tbot
```

Log out, reconnect as `tbot`, and confirm:

```
$ docker run hello-world
$ docker compose version
```

(Note the space: `docker compose`, the plugin — not the old standalone `docker-compose`
binary. `deploy/systemd/tbot-recorder.service` uses the plugin form.)

---

## 8. Generate a read-only GitHub deploy key — ON THE SERVER

This is a **different** key pair from the one in section 4 (that one is for Parham to log
into the server; this one is for the server to read the private repo from GitHub). Do **not**
use a personal access token (CLAUDE.md section 3.6 and this task's brief both rule that out)
— a deploy key can be scoped read-only to exactly this one repo.

As `tbot`, on the server:

```
$ ssh-keygen -t ed25519 -C "tbot-vps-deploy-key" -f ~/.ssh/github_deploy_key -N ""
$ cat ~/.ssh/github_deploy_key.pub
```

Copy that public key's output, then on GitHub: open the `trade-bot` repo → **Settings** →
**Deploy keys** → **Add deploy key** → paste it, give it a name like `turkey-vps`, and leave
**"Allow write access" unchecked** (read-only). Save.

Back on the server, tell SSH to use this key specifically for GitHub:

```
$ cat >> ~/.ssh/config << 'EOF'
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/github_deploy_key
  IdentitiesOnly yes
EOF
$ chmod 600 ~/.ssh/config
```

Verify GitHub accepts it (it will print a "successfully authenticated" message and then
refuse a shell — that's expected and fine):

```
$ ssh -T git@github.com
```

---

## 9. Clone the repo and create `.env`

```
$ sudo mkdir -p /opt/tbot
$ sudo chown tbot:tbot /opt/tbot
$ cd /opt/tbot
$ git clone git@github.com:parhamkhm/trade-bot.git
$ cd trade-bot
$ git checkout main
$ git log --oneline -1
```

The server always runs `main`. Feature branches are merged there only after review, so do
not check out a feature branch on the server.

Create `.env` from the committed template — **never** commit `.env` itself:

```
$ cp .env.example .env
$ chmod 600 .env
```

Leave the Tabdeal values empty for now — section 11 explains exactly what to put in them,
and in what order (the server's IP has to exist before the API key can be whitelisted to it).

Also create the `recorder` service's **own**, separate, non-secret env file:

```
$ cp deploy/recorder.env.example deploy/recorder.env
```

This one holds no credentials at all and does not need `chmod 600` — it exists so that the
`recorder` container (`docker-compose.yml`'s `recorder` service) never receives the
Tabdeal/Telegram secrets in `.env` in the first place: it only calls Tabdeal's public,
unsigned endpoints, so it has no business being able to see those values in its own
environment or in `docker inspect tbot-recorder`. Both files are required — `docker compose
config` (section 13) fails immediately if either is missing, the same way it would if plain
`.env` were missing.

---

## 10. Find the server's static IP (needed for the Tabdeal API key whitelist)

```
$ curl -4 ifconfig.me
```

Write this IP address down — you need it in the next section, and it will not change unless
the VPS provider reassigns it.

---

## 11. Create the Tabdeal API key

Do this from the Tabdeal account panel (tabdeal.org), **after** you have the IP from section
10:

1. Log into your Tabdeal account and find the API key management page.
2. Create a **new** API key with:
   - **Read-only** permission.
   - **Withdrawal permission: disabled.**
   - **Trading permission: disabled** (phase 0–5 of this project never sends orders —
     CLAUDE.md section 3.6 — so the key should not even be able to).
   - **IP whitelist: restricted to exactly the IP from section 10** (not "any IP").
3. Tabdeal will show the key and secret **once**. Copy both immediately.
4. Put them in `.env` on the server (still `chmod 600` from section 9):

   ```
   $ nano /opt/tbot/trade-bot/.env
   ```

   Fill in:

   ```
   TBOT_TABDEAL_API_KEY=<the key>
   TBOT_TABDEAL_API_SECRET=<the secret>
   ```

   Leave `TBOT_LIVE_TRADING=false` — this is a hard safety switch (CLAUDE.md section 3.6);
   nothing in phases 0–5 needs it set to `true`, and no step in this document asks you to
   change it.

Never paste these values into a chat, an issue, a commit, or a log line. `.env` on the server,
`chmod 600`, is the only place they belong.

---

## 12. Run the read-only Tabdeal probe

The probe itself (`scripts/tabdeal_probe.py`) is built by a separate task and documented in
its own report — this is how you invoke it once it exists:

```
$ cd /opt/tbot/trade-bot
$ uv sync --locked
$ uv run python -m scripts.tabdeal_probe --help
```

Typical use (adjust flags to whatever `--help` actually lists):

```
$ uv run python -m scripts.tabdeal_probe --samples 30 --interval 2 --out research/reports/tabdeal_probe_$(date -u +%Y%m%dT%H%M%SZ).json
```

Run it **from this server** (Turkey), not from a laptop that might be in Iran — a network
path that's blocked or throttled would otherwise look like a Tabdeal problem when it isn't
(see `docs/reports/phase0-1-tasks.md` point 5). The private-account part of the probe only
runs once `.env` has both `TBOT_TABDEAL_API_KEY` and `TBOT_TABDEAL_API_SECRET` set (section
11); without them it still runs the public checks.

---

## 13. Build and start the recorder service

First, a one-time sanity check that the Compose file itself is valid:

```
$ cd /opt/tbot/trade-bot
$ docker compose -f deploy/docker-compose.yml config
```

This should print the fully-resolved configuration with no errors. (This command could not
be run from the Windows machine that wrote this document — only YAML-syntax-checked with a
Python parser, not Compose-schema-checked. Running it here, on the real server, with the
real `docker compose` plugin, is the real test. If it errors, stop and report the exact
message rather than guessing at a fix.)

Install the systemd unit so the recorder starts on boot and restarts if `docker compose up`
itself ever fails:

```
$ sudo cp deploy/systemd/tbot-recorder.service /etc/systemd/system/
$ sudo systemctl daemon-reload
$ sudo systemctl enable --now tbot-recorder.service
```

Check it came up:

```
$ sudo systemctl status tbot-recorder.service
$ docker compose -f deploy/docker-compose.yml ps
```

The `recorder` container should show as `Up` and, after roughly `start_period` (30s) plus one
health-check interval, `healthy`. "Healthy" here means **two** things now, both checked by
`deploy/healthcheck.py` (run by `deploy/Dockerfile`'s `HEALTHCHECK`):

1. The heartbeat file (`TBOT_HEARTBEAT_FILE`) has a `last_poll_ts` newer than
   `TBOT_HEARTBEAT_MAX_AGE_SECONDS` ago — the process is still alive and cycling.
2. Its `consecutive_errors` field is below `TBOT_HEARTBEAT_MAX_CONSECUTIVE_ERRORS`
   (default 10) — the poll cycle is actually *succeeding*, not just running.

This second check matters because the recorder rewrites the heartbeat file on **every**
poll cycle, including one whose poll itself failed — so a recorder that is up but failing
every single poll (bad network path, Tabdeal down, a bug) still has a fresh-looking file.
Before this fix-round, the healthcheck only looked at the file's mtime, so exactly that
failure mode showed as `healthy` indefinitely; now `unhealthy` can mean either "the process
is gone/wedged" (check 1) or "the process is up but every poll is failing" (check 2) — run
```
$ docker compose -f deploy/docker-compose.yml logs --since 1h recorder
```
or `cat` the heartbeat file directly (see "Where the data lives" below) to tell which one
you're looking at. If the recorder's actual heartbeat path ends up different from the
Dockerfile's default (`/app/data/tabdeal/heartbeat.json`), update that `ENV` line (and
re-deploy) to match — ask whoever built `src/tbot/data/tabdeal_recorder.py` what path it
actually uses if unsure.

---

## 14. Day-to-day: start, stop, inspect, logs

All of these can be run either via the systemd unit or via `docker compose` directly; both
affect the same containers.

```
$ sudo systemctl stop tbot-recorder.service      # stop the whole managed project
$ sudo systemctl start tbot-recorder.service      # start it again
$ sudo systemctl restart tbot-recorder.service

$ docker compose -f deploy/docker-compose.yml ps                  # status + health
$ docker compose -f deploy/docker-compose.yml logs -f recorder    # follow logs live
$ docker compose -f deploy/docker-compose.yml logs --since 1h recorder
$ docker inspect --format '{{json .State.Health}}' tbot-recorder  # raw health-check history
```

### Daily health check — do this by hand until phase 5

Nothing alerts you yet: Telegram arrives in phase 5, and `restart: unless-stopped` only
reacts when the process **exits**, not when the container turns `unhealthy`. A recorder that
is up but failing every poll stays up, failing, until someone looks. Until alerting exists,
run this once a day (it takes seconds):

```
$ docker inspect --format '{{.State.Health.Status}}' tbot-recorder
$ sudo cat "$(docker volume inspect tbot-data --format '{{ .Mountpoint }}')/tabdeal/heartbeat.json"
```

The first line must print `healthy`. If it prints `unhealthy`, read the last hour of logs
(`docker compose -f deploy/docker-compose.yml logs --since 1h recorder`) before restarting
anything — a restart does not fix a Tabdeal outage or a blocked network path, and every
unhealthy hour counts against gate G1b's "≥ 99 % of hours complete".

Logs are structured JSON (one object per line — `src/tbot/monitoring/logging.py`), rotated by
Docker itself per `deploy/docker-compose.yml`'s `logging:` block (10 MB × 5 files per
container, then the oldest is dropped — it will never silently fill the disk, but it also
only keeps recent history; for anything you need to keep long-term, copy it off the server).

### Where the data lives

The recorder writes into a **named Docker volume** (`tbot-data`), not a plain directory, so
on the host its files live under Docker's own storage area — find the exact path with:

```
$ docker volume inspect tbot-data --format '{{ .Mountpoint }}'
```

That path (typically something like `/var/lib/docker/volumes/tbot-data/_data`) contains:

- `tabdeal/trades.sqlite` — the raw trades/poll-log/gaps/order-book SQLite database
  (SPEC section 5.4).
- `parquet/klines/source=tabdeal/...` — the built 1h candles.
- `tabdeal/heartbeat.json` (or wherever `TBOT_HEARTBEAT_FILE` points, see section 13) — the
  liveness file the Docker healthcheck watches.

Reading or copying anything under that path requires `sudo` (the volume's files are owned by
the container's internal UID, not necessarily `tbot`).

---

## 15. Copying the Parquet store to/from Parham's Windows laptop

Binance may be unreachable or throttled from Iran, so this server-to-laptop (and back) path
matters: research can run on the server and the Parquet store can be pulled to the laptop,
or a store built on the laptop (wherever Binance *is* reachable) can be pushed up.

Both methods below need the volume's real host path from section 14 — grab it once and reuse
it:

```
$ VOL=$(docker volume inspect tbot-data --format '{{ .Mountpoint }}')
$ echo "$VOL"
```

### Server → Windows laptop

From **PowerShell on the Windows laptop** (Windows 10/11 ship `scp` and an SSH client by
default):

```
C:\> scp -P <ssh-port> -r tbot@<server-ip>:/var/lib/docker/volumes/tbot-data/_data/parquet C:\trade-bot-data\parquet
```

(Substitute the real `$VOL` path from above if it differs from the typical one shown.)
`scp -r` is simple but restarts from scratch if interrupted — fine for a one-off pull, painful
for a large store over a slow or flaky link.

`rsync` resumes interrupted transfers and only re-sends changed bytes, which matters more as
the store grows, but Windows has no built-in `rsync`. The practical options, in order of
least friction:

- **Easiest:** just use `scp -r` above for occasional, complete-enough-to-wait-for transfers.
- **If you need resumable syncing regularly:** install WSL (`wsl --install`, one-time, needs a
  reboot) and run `rsync` from inside it, pointing at the Windows filesystem via
  `/mnt/c/...`:

  ```
  C:\> wsl rsync -avz --partial --progress -e "ssh -p <ssh-port>" tbot@<server-ip>:/var/lib/docker/volumes/tbot-data/_data/parquet/ /mnt/c/trade-bot-data/parquet/
  ```

Either way, since reading the volume on the server needs `sudo`, you may need to first copy
it server-side into a directory `tbot` owns, then `scp`/`rsync` from there:

```
$ sudo rsync -a "$VOL/parquet" /home/tbot/parquet-export/
$ sudo chown -R tbot:tbot /home/tbot/parquet-export
```

and then pull from `/home/tbot/parquet-export/parquet` instead of the raw volume path.

### Windows laptop → server (pushing a store built where Binance is reachable)

From PowerShell:

```
C:\> scp -P <ssh-port> -r C:\trade-bot-data\parquet tbot@<server-ip>:/home/tbot/parquet-import
```

or, with `rsync` via WSL:

```
C:\> wsl rsync -avz --partial --progress -e "ssh -p <ssh-port>" /mnt/c/trade-bot-data/parquet/ tbot@<server-ip>:/home/tbot/parquet-import/
```

Then, on the server, move it into the volume (recorder stopped first so nothing is mid-write):

```
$ sudo systemctl stop tbot-recorder.service
$ sudo rsync -a /home/tbot/parquet-import/ "$VOL/parquet/"
$ sudo systemctl start tbot-recorder.service
```

---

## 16. Checklist — what Parham must do by hand

Nothing above runs itself; in particular, none of this was executed against a real server
from the machine that wrote it (no Docker/Linux available there — see the note at the top).
Before considering the server "done":

- [ ] Ran section 1 (`ss -tlnp`) and knows the real SSH port and every other listening
      service on this specific VPS.
- [ ] Decided, for anything found in section 1 besides SSH, whether it needs a `ufw allow`
      rule — and added it **before** enabling ufw.
- [ ] Completed sections 3–6 in order, with a working key-based `tbot` login **verified in a
      separate session** before password auth was disabled, and **verified again** after
      the firewall was enabled.
- [ ] Confirmed the clock offset is under 1 second (section 2) — not just that an NTP service
      is "active", but the actual printed offset number.
- [ ] Added the deploy key's public half to the GitHub repo's **Deploy keys** (read-only) —
      not a personal access token.
- [ ] Created the Tabdeal API key with the IP from section 10 whitelisted, withdrawal and
      trading both disabled, and pasted it into `.env` (`chmod 600`) — never anywhere else.
- [ ] Ran the probe (section 12) from the server itself and confirmed `BTCUSDT` is `TRADING`
      with a sane spread, per gate G0 in `docs/SPEC.md`.
- [ ] Ran `docker compose -f deploy/docker-compose.yml config` on the real server and
      confirmed it prints valid output (this document's author could not run this check).
- [ ] Confirmed the `recorder` container reaches `healthy`, and knows that "healthy" now
      means BOTH "the heartbeat file is fresh" AND "`consecutive_errors` is below
      threshold" (section 13) — "unhealthy" can mean the process is gone, or that it's up
      but every poll is failing; check the heartbeat file / logs to tell which. If
      `src/tbot/data/tabdeal_recorder.py` (built separately) uses a different heartbeat
      path/convention than `deploy/Dockerfile` assumes, reconciled the two.
- [ ] Created `deploy/recorder.env` from `deploy/recorder.env.example` (section 9) — the
      `recorder` container's own, non-secret env file, separate from the repo-root `.env`
      that holds the Tabdeal/Telegram credentials.
- [ ] Knows the real Tabdeal maker/taker fee tier isn't needed for this step, but is still an
      open question in `docs/SPEC.md` section 9 — unrelated to server setup, but worth not
      forgetting.
- [ ] Decided whether CI needs a self-hosted runner or GitHub-hosted is fine for a private
      repo (`docs/SPEC.md` section 9, question 7) — this document does not touch CI.

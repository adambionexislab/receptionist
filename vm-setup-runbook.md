# Apollonia voice VM: setup runbook

Step-by-step setup of Apollonia's own Hetzner VM (Asterisk 22 in Docker + the agent's user, env file and service) for the ElevenLabs voice. The plan and the reasons behind every choice are in [elevenlabs-voice-roadmap.md](elevenlabs-voice-roadmap.md). That document wins if the two ever disagree.

This runbook covers **roadmap Phase 2 infrastructure**. It can be done alongside Phase 0/1, because nothing in production uses this VM until Phase 3, when the demo DID is moved.

> **Status (2026-10-19): first pass (§2–§9) done on the real VM.** This runbook now records what was actually built. The deployed Asterisk files are in the repo under [`voiceagent/pbx/`](voiceagent/pbx/) (`Dockerfile`, `compose.yaml`, `etc/*.conf`, copied from the VM), and **they are the source of truth for §4–§6**. If a snippet here ever differs from them, the repo files win; update this runbook to match. Section **"Build notes"** at the end of each part records what differed from the original plan and why. The open item for roadmap Phase 4 is in §10.

---

## 0. Instructions for the guiding Claude

You are guiding a human through this runbook on their machines. Rules:

1. **One step at a time.** Give the commands for one step, wait for the user to paste the output, check it against the step's **Expected**, and only then move on. On anything unexpected, stop and diagnose before continuing.
2. **Never ask for secrets in chat.** API keys and tokens are typed straight into files on the server (`nano`) or generated there. If the user pastes a secret anyway, tell them to rotate it.
3. **The call recording assistant's box is off limits.** Its only use here was one read-only step (§4.1: `cat`/`ls`). Never run anything else there: no installs, no `docker build`, no restarts, no config or firewall changes.
4. **Production is untouched.** Don't change the existing DIDWW → OpenAI trunk, the call assistant's DIDWW trunks, any production DID, or Render. The only DIDWW changes in this runbook are a **new** trunk and a **spare test DID** (§9).
5. Use the values in §1. For a rebuild, deploy the files from `voiceagent/pbx/` rather than retyping the snippets.
6. When a step is done, the user ticks it off in this file.

---

## 1. Values

| Name | Value | Source |
|---|---|---|
| VM | `apollonia-voice`, Hetzner **nbg1**, **Ubuntu 26.04.1 LTS**, system **Python 3.14.4** | Hetzner panel / VM |
| `<VM_IP>` | **91.99.238.181** | Hetzner panel |
| `<ADMIN_IP>` | the user's own public IP(s) for SSH | e.g. `curl ifconfig.me` on the user's PC |
| `<DIDWW_SIP_IPS>` | the **7 Static Endpoint IPv4 addresses** from DIDWW's SIP connectivity reference (updated 14 Oct 2026): `46.19.209.14`, `46.19.210.14`, `46.19.212.14`, `46.19.213.14`, `46.19.214.14`, `46.19.215.14`, `185.238.173.14` | used in the Hetzner firewall, ufw and the pjsip `identify`. If DIDWW updates its list, change all three places |
| `<OPENAI_PROJECT_ID>` | the project ID in today's OpenAI SIP URI (see `voiceagent/pbx/etc/pjsip.conf`, `[openai-sip]` aor) | the existing DIDWW → OpenAI trunk's destination |
| `<LOCATION>` | nbg1 | same as the call assistant's box |
| `<RENDER_URL>` | the Render API base URL | Render dashboard |
| `<TEST_DID>` | **421482304191** on the new DIDWW trunk **`apollonia-eleven`**; owned by the test tenant "Apollonia Voice Test" (`voice_engine='realtime'`) | DIDWW panel / admin |

Secrets (never in chat, never in this file): `ELEVENLABS_API_KEY`, the OpenAI agent key, `VOICE_AGENT_TOKEN`. Keep the originals in a password manager.

---

## 2. Create the VM (Hetzner panel)

> **Done 2026-10-19:** `apollonia-voice`, nbg1, Ubuntu 26.04.1 LTS, 91.99.238.181. For a rebuild, the items below are the spec.

- [x] **Server:**
  - **Type:** shared vCPU with **2 vCPU / 4 GB** (the CX or CPX line, whichever current type has 2 vCPU / 4 GB).
  - **Location:** nbg1, the same as the call assistant's box.
  - **Image:** Ubuntu 26.04 (the VM runs 26.04.1 LTS).
  - **SSH key:** the user's key; **no password**.
  - **Name:** `apollonia-voice`.
  - **Backups:** **off** for now; they're turned on in Phase 3 (§10).
- [x] **Cloud firewall** `apollonia-voice-fw`, attached to the server. Inbound rules only, so all outbound stays allowed:

  | Protocol | Port | Source | Why |
  |---|---|---|---|
  | TCP | 22 | `<ADMIN_IP>` | SSH |
  | UDP | 5060 | the 7 DIDWW IPs (§1) | SIP from DIDWW only |
  | UDP | 10000–10999 | **any** (0.0.0.0/0, ::/0) | RTP. **Must be open to any source:** on fallback calls the audio comes from OpenAI's media servers, not DIDWW (roadmap §2.4) |

  **No** 5061 inbound, no softphone rules.

**Expected:** server running; `ssh root@91.99.238.181` works with the key.

---

## 3. Base system (on the VM)

The host runs **Ubuntu 26.04.1 LTS**. Where this runbook isn't certain that something behaves on 26.04 as it did on earlier releases, it gives a **"verify on the VM:"** command. Run it first; if the output contradicts the step, stop and adapt.

### 3.1 Updates and basics
Verify on the VM: `lsb_release -ds && apt-cache policy unattended-upgrades fail2ban ufw | grep -E '^[a-z]|Candidate'` (every package must show a `Candidate:` version, not `(none)`).
```bash
apt update && apt -y full-upgrade
apt -y install unattended-upgrades fail2ban ufw curl ca-certificates jq
timedatectl set-timezone Europe/Bratislava
hostnamectl set-hostname apollonia-voice
reboot
```
Verify on the VM: `systemctl is-enabled unattended-upgrades && systemctl is-active unattended-upgrades` (both should be `enabled` / `active`).

**Expected:** the VM comes back; `ssh` works again.

**Known on this VM (harmless, leave it):** `cloud-init-main` reports a failure on every boot. It looks for an interface called `enp1s0`, but the interface is `eth0`. Networking is unaffected, because netplan matches the interface by MAC address. **Leave cloud-init enabled**; don't "fix" this by disabling it.

### 3.2 SSH: keys only
Verify on the VM: `systemctl list-units --all 'ssh*' --no-pager` (shows the SSH unit names on 26.04, `ssh.service` and/or `ssh.socket`; use those names below).
Verify on the VM: `grep -n '^Include' /etc/ssh/sshd_config` (should show `Include /etc/ssh/sshd_config.d/*.conf` **before** other settings, so a drop-in is read).

Check the setting **sshd actually uses** (merged from all files):
```bash
sshd -T | grep -Ei '^(passwordauthentication|permitrootlogin)'
```
If `passwordauthentication` isn't `no`:
```bash
echo 'PasswordAuthentication no' > /etc/ssh/sshd_config.d/10-nopassword.conf
sshd -t && systemctl reload ssh
sshd -T | grep -i '^passwordauthentication'
```
In `sshd_config.d`, the first value read wins, and files are read in name order, so `10-…` comes before any cloud-init `50-…` file.

Verify on the VM: `systemctl status ssh --no-pager`. If `reload` reports the unit as inactive (socket activation), use `systemctl restart ssh` instead; existing sessions stay open.

**Expected:** `passwordauthentication no`, and a **second** terminal can still log in with the key **before** the first one is closed.

### 3.3 ufw (host firewall, same rules as the cloud firewall)
Verify on the VM: `iptables -V` (should say `nf_tables`: ufw writes its rules through the iptables-nft layer into nftables).
```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow from <ADMIN_IP> to any port 22 proto tcp
for ip in 46.19.209.14 46.19.210.14 46.19.212.14 46.19.213.14 46.19.214.14 46.19.215.14 185.238.173.14; do
  ufw allow from "$ip" to any port 5060 proto udp
done
ufw allow 10000:10999/udp
ufw enable
ufw status numbered
```
Asterisk runs in Docker with **host networking** (§5). Because no ports are published, Docker creates no port-forwarding rules and doesn't bypass ufw here.

Verify on the VM (after §3.4, once Docker runs): `nft list ruleset | grep -c ufw` (should be > 0, so ufw's rules are still present after Docker added its own) and `ufw status verbose` (still `Status: active` with the same rules).

**Expected:** the rules listed exactly as above (SSH, 7 × 5060/udp from DIDWW, RTP range); SSH still works.

### 3.4 Docker
Verify on the VM: `apt-cache policy docker.io docker-compose-v2 docker-buildx | grep -E '^[a-z]|Candidate'`. If any shows `Candidate: (none)` on 26.04, install from Docker's own apt repository instead (`docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-compose-plugin`, `docker-buildx-plugin`). First check that download.docker.com publishes packages for this release: `. /etc/os-release; echo $VERSION_CODENAME`, then look for that name under `https://download.docker.com/linux/ubuntu/dists/`.
```bash
apt -y install docker.io docker-compose-v2 docker-buildx
systemctl enable --now docker
docker version && docker compose version
```
**Expected:** both print versions without errors.

The Asterisk image is built `FROM debian:trixie` inside Docker (§4), so the host's Ubuntu release doesn't affect it.

---

## 4. Asterisk 22 image

**Source of truth:** [`voiceagent/pbx/Dockerfile`](voiceagent/pbx/Dockerfile).

### 4.1 Template: the call assistant's Dockerfile (read-only)
On the **call assistant's box**, read only:
```bash
ls -la /opt/pbx
cat /opt/pbx/Dockerfile
```
Done 2026-10-19. That box was not otherwise touched.

### 4.2 What was built: the call assistant's Dockerfile, adapted
The call assistant's Dockerfile was **adapted**. The reference Dockerfile from the first version of this runbook was **not used** and has been removed. The result (see the repo file) is:
- **Base:** Debian **trixie** (`debian:trixie-20260918-slim`, pinned), two stages: build and runtime.
- **Asterisk 22.11.0**, downloaded from downloads.asterisk.org and **verified against a pinned SHA-256** before unpacking.
- **Added `func_curl`** (`libcurl4-openssl-dev` at build time); needed by the dialplan (§6.3).
- **Modules checked at build time:** the build **fails** if `res_pjsip`, `res_srtp`, `res_resolver_unbound`, `func_curl`, `res_audiosocket` or `chan_audiosocket` is missing.
- **Runtime packages are derived automatically** from what the built binaries link against (`ldd` → `dpkg -S`), plus `ca-certificates` (TLS to OpenAI) and `tzdata`.
- **Runs as a non-root user `asterisk`, UID 10002** (`USER asterisk`).
- **Removed** from the template: the recording notice, sox/espeak, python3 at runtime, the PBX scripts and the entrypoint. Config is **mounted** (§5), not baked in.
- No sound packs (core sounds, MOH, extra sounds disabled).

### 4.3 Build
Copy the files from the repo to the VM (from the user's PC):
```bash
ssh root@91.99.238.181 'mkdir -p /opt/apollonia-pbx/etc /opt/apollonia-pbx/logs'
scp voiceagent/pbx/Dockerfile voiceagent/pbx/compose.yaml root@91.99.238.181:/opt/apollonia-pbx/
scp voiceagent/pbx/etc/*.conf root@91.99.238.181:/opt/apollonia-pbx/etc/
```
On the VM:
```bash
chown -R 10002:10002 /opt/apollonia-pbx/logs     # the container runs as UID 10002 and must write its logs
cd /opt/apollonia-pbx
docker build -t apollonia-pbx:22 .
```
This takes 10–20 minutes on 2 vCPUs. The build log ends with the list of runtime packages.

**Expected:** the build finishes without `MISSING MODULE`; `docker images | grep apollonia-pbx` lists `apollonia-pbx:22`.

**Build notes:**
- The `logs` directory **must be owned by UID 10002**. Otherwise Asterisk (non-root) can't write `messages.log`, and the fail2ban `asterisk` jail has nothing to read.
- `etc` doesn't need to be writable (it's mounted read-only, §5); it only has to be readable.

---

## 5. Compose file

**Source of truth:** [`voiceagent/pbx/compose.yaml`](voiceagent/pbx/compose.yaml).
```yaml
services:
  asterisk:
    image: apollonia-pbx:22
    container_name: apollonia-pbx
    network_mode: host
    restart: unless-stopped
    volumes:
      - ./etc:/etc/asterisk:ro
      - ./logs:/var/log/asterisk
```
**Build note:** the config is mounted **read-only** (`:ro`). Asterisk never writes to its own config, and changes are made in the repo, copied over, and applied with `dialplan reload` / `pjsip reload`.

---

## 6. Asterisk configuration (`/opt/apollonia-pbx/etc/`)

**Source of truth:** [`voiceagent/pbx/etc/`](voiceagent/pbx/etc/). Only these files are mounted. Anything not listed falls back to Asterisk's built-in defaults.

### 6.1 `pjsip.conf`
```ini
[global]
type=global
user_agent=Apollonia
endpoint_identifier_order=ip,username

[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060

; TLS transport for OUTBOUND calls to OpenAI (inbound 5061 is closed by the firewall)
[transport-tls]
type=transport
protocol=tls
bind=0.0.0.0:5061
method=tlsv1_2
ca_list_file=/etc/ssl/certs/ca-certificates.crt
verify_server=yes
allow_wildcard_certs=yes   ; OpenAI cert: CN=api.openai.com, SAN *.api.openai.com

; -- DIDWW -> Apollonia (the new Eleven trunk) --
[didww-eleven]
type=endpoint
transport=transport-udp
context=from-didww-eleven
disallow=all
allow=alaw,ulaw
direct_media=no
rtp_symmetric=yes

[didww-eleven-identify]
type=identify
endpoint=didww-eleven
match=46.19.209.14
match=46.19.210.14
match=46.19.212.14
match=46.19.213.14
match=46.19.214.14
match=46.19.215.14
match=185.238.173.14

; -- Asterisk -> OpenAI SIP (the fallback) --
[openai-sip]
type=endpoint
transport=transport-tls
context=from-openai          ; OpenAI never calls in; this context stays empty
disallow=all
allow=ulaw,alaw
aors=openai-sip
direct_media=no
media_encryption=sdes        ; matches the existing DIDWW -> OpenAI trunk (SRTP SDES)

[openai-sip]
type=aor
contact=sip:<OPENAI_PROJECT_ID>@sip.api.openai.com:5061;transport=tls
```
(The repo file has the real project ID in the `contact` line.)

**Build notes:**
- **`media_encryption=sdes`:** decided up front, because the existing DIDWW → OpenAI trunk uses **TLS on 5061 + SRTP (SDES)**. This settles the roadmap §2.5 SRTP check.
- **`allow_wildcard_certs=yes`** (with `verify_server=yes` kept): OpenAI's certificate has **CN=api.openai.com** and **SAN `*.api.openai.com`**. Without this option PJSIP rejected it with **"server identity does not match"** when connecting to `sip.api.openai.com`. Verification stays on: the certificate chain and the wildcard name are still checked.
- **The identify lists the 7 DIDWW Static Endpoint IPv4 addresses** (§1).

### 6.2 `rtp.conf`
```ini
[general]
rtpstart=10000
rtpend=10999
strictrtp=yes
```

### 6.3 `extensions.conf`
```ini
[globals]
AGENT_URL=http://127.0.0.1:9093/calls

[from-didww-eleven]
; ${EXTEN} is the DID (DIDWW's default Request-URI user)
exten => _X.,1,NoOp(Eleven inbound ${CALLERID(num)} -> ${EXTEN})
 same => n,Set(DIV=${PJSIP_HEADER(read,Diversion)})
 ; ONE request, to the local agent. Plain-text answer: decline | realtime | ok:<uuid>
 ; timeout or error -> "" -> fallback (roadmap §4.3)
 same => n,Set(CURLOPT(conntimeout)=1)
 same => n,Set(CURLOPT(httptimeout)=2)
 same => n,Set(REG=${CURL(${AGENT_URL},from=${URIENCODE(${CALLERID(num)})}&to=${URIENCODE(${EXTEN})}&diversion=${URIENCODE(${DIV})})})
 same => n,GotoIf($["${REG}" = "decline"]?busy)
 same => n,GotoIf($["${CUT(REG,:,1)}" != "ok"]?fallback)
 same => n,Set(UUID=${CUT(REG,:,2)})
 same => n,Dial(AudioSocket/127.0.0.1:9092/${UUID})
 same => n,GotoIf($["${DIALSTATUS}" != "ANSWER"]?fallback)
 same => n,Hangup()
 same => n(busy),Hangup(17)                                   ; -> 486, never re-routed by DIDWW
 same => n(fallback),Dial(PJSIP/openai-sip,3,b(add-diversion^s^1(${EXTEN})))
 same => n,GotoIf($["${DIALSTATUS}" = "ANSWER"]?done)
 same => n,GotoIf($["${DIALSTATUS}" = "BUSY"]?busy)           ; OpenAI side declined (personal number)
 same => n,Hangup(34)                                         ; -> 503: DIDWW re-routes to the OpenAI trunk
 same => n(done),Hangup()

; Fallback leg: put the DID in Diversion. On DIDWW calls today OpenAI sees its
; project id in To, and the router finds the tenant from Diversion
; (backend/call/router.py, _resolve_call). The caller stays in From.
[add-diversion]
exten => s,1,Set(PJSIP_HEADER(add,Diversion)=<sip:${ARG1}@91.99.238.181>\;reason=unconditional)
 same => n,Return()

[from-openai]
; intentionally empty
```
Until the agent exists, the `CURL` gets no answer, so **every call takes the fallback**. That's how §9.3 tested the fallback path first.

**Build notes:**
- **`\;reason=unconditional` must be escaped.** In `extensions.conf` a `;` starts a comment, so without the backslash everything from `;reason` onward is dropped. The `Set` line is then cut off, **no Diversion header is sent**, and OpenAI / Render answer **404** (no tenant found).
- The host in the Diversion URI is the VM's IP (`91.99.238.181`). Only the user part (the DID) matters to the router.
- `Hangup(34)` (503) depends on DIDWW re-routing on 503, which isn't in DIDWW's default re-routing codes. It's an open item before the trunk group goes live (§10, roadmap Phase 4).

### 6.4 `logger.conf`
```ini
[general]
dateformat=%F %T

[logfiles]
console => notice,warning,error,verbose
messages.log => notice,warning,error,security
```

### 6.5 `modules.conf`
```ini
[modules]
autoload=yes
```

---

## 7. Start Asterisk and verify
```bash
cd /opt/apollonia-pbx
docker compose up -d
docker logs --tail 50 apollonia-pbx
A="docker exec apollonia-pbx asterisk -rx"
$A "core show version"
$A "module show like audiosocket"
$A "module show like curl"
$A "module show like srtp"
$A "pjsip show transports"
$A "pjsip show endpoints"
$A "pjsip show identifies"
$A "dialplan show from-didww-eleven"
ss -ulnp | grep 5060
```
**Expected:**
- `Asterisk 22.11.0`;
- `res_audiosocket` + `chan_audiosocket`, `func_curl` and `res_srtp` loaded (`Running`);
- both transports listed;
- endpoints `didww-eleven` and `openai-sip`; the identify listing the 7 DIDWW IPs;
- the dialplan printed without errors;
- Asterisk listening on UDP 5060;
- `/opt/apollonia-pbx/logs/messages.log` exists and grows (written by UID 10002).

To apply config changes later: copy the changed file from `voiceagent/pbx/etc/`, then `$A "dialplan reload"` or `$A "pjsip reload"`. No rebuild or container restart is needed.

### 7.1 fail2ban
Verify on the VM: `ls -l /var/log/auth.log; systemctl is-active rsyslog` (tells whether 26.04 writes SSH logins to `/var/log/auth.log` or only to the systemd journal).
Verify on the VM: `apt-cache policy python3-systemd | grep Candidate` (fail2ban needs this package to read the journal).

`/etc/fail2ban/jail.d/apollonia.local`:
```ini
[sshd]
enabled = true
; Read from the systemd journal. Required if /var/log/auth.log doesn't exist;
; it also works when it does, so it's set either way.
backend = systemd

[asterisk]
enabled = true
; The asterisk jail reads a FILE, so it must not inherit the systemd backend
backend = auto
logpath = /opt/apollonia-pbx/logs/messages.log
```
```bash
apt -y install python3-systemd
systemctl restart fail2ban
fail2ban-client status
fail2ban-client status sshd
```
Verify on the VM: `fail2ban-client -d 2>/dev/null | grep -i "banaction" | head -3` (shows which ban action fail2ban uses: an `iptables-*` action goes through the iptables-nft layer, `nftables-*` writes to nftables directly; both end up in nftables on 26.04). After a first ban, `nft list ruleset | grep -i f2b` should show fail2ban's set or chain.

**Expected:** jails `sshd` and `asterisk` listed; `fail2ban-client status sshd` shows the journal as its source, not a file path.

**Result on `apollonia-voice` (2026-10-19):** both jails running: `sshd` with `backend = systemd`, `asterisk` on `messages.log`.

---

## 8. Agent user, env file and service (the API keys go here)

The agent code doesn't exist yet (roadmap §4.2). This step prepares everything around it: the keys go into place now, and the service is enabled once the code is installed.

### 8.1 User and directories
```bash
useradd --system --no-create-home --shell /usr/sbin/nologin apollonia-agent
mkdir -p /opt/apollonia-agent
install -d -o apollonia-agent -g apollonia-agent -m 750 /var/lib/apollonia-agent
```

### 8.2 Env file: readable only by the agent user
```bash
install -o apollonia-agent -g apollonia-agent -m 600 /dev/null /etc/apollonia-agent.env
openssl rand -hex 32        # → the VOICE_AGENT_TOKEN value (copy it into the password manager)
nano /etc/apollonia-agent.env
```
Contents (the user types the secrets **directly in nano**, never in chat):
```
OPENAI_API_KEY=<agent-specific OpenAI key>
OPENAI_REALTIME_MODEL=gpt-realtime-2.1
ELEVENLABS_API_KEY=<the telephony ElevenLabs key>
ELEVENLABS_MODEL=eleven_v4_turbo
ELEVENLABS_WS_URL=wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input
ELEVENLABS_VOICE_ID_SK=WtA85syCrJwasGeHGH2p
ELEVENLABS_VOICE_ID_IT=
ELEVENLABS_PRONUNCIATION_DICT_ID=
ELEVENLABS_PRONUNCIATION_DICT_VERSION=
ELEVENLABS_OUTPUT_FORMAT=pcm_8000
RENDER_API_BASE=<RENDER_URL>
VOICE_AGENT_TOKEN=<value from openssl above>
AUDIOSOCKET_BIND=127.0.0.1:9092
AGENT_HTTP_BIND=127.0.0.1:9093
```
```bash
ls -l /etc/apollonia-agent.env
```
**Expected:** `-rw------- 1 apollonia-agent apollonia-agent … /etc/apollonia-agent.env`.

The same `VOICE_AGENT_TOKEN` goes on Render in **roadmap Phase 1**, with the nightly deploy. **Don't** add it to Render from this runbook: changing env vars on Render can trigger a deploy that drops live calls.

### 8.3 systemd unit (installed now, **not enabled** until the agent code exists)
`/etc/systemd/system/apollonia-agent.service`:
```ini
[Unit]
Description=Apollonia voice agent (AudioSocket ↔ gpt-realtime ↔ ElevenLabs)
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=apollonia-agent
Group=apollonia-agent
EnvironmentFile=/etc/apollonia-agent.env
WorkingDirectory=/opt/apollonia-agent
ExecStart=/opt/apollonia-agent/venv/bin/python -m voiceagent
Restart=always
RestartSec=2
StateDirectory=apollonia-agent
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/apollonia-agent

[Install]
WantedBy=multi-user.target
```
```bash
systemctl daemon-reload
systemctl status apollonia-agent --no-pager   # "loaded", "inactive (dead)" is correct for now
```
**Python for the agent:** the agent runs on the VM's **system Python 3.14** (3.14.4 on this VM, Ubuntu 26.04), in a venv at **`/opt/apollonia-agent/venv`**. That's the path the unit's `ExecStart` uses. The venv is created when the code is installed:
```bash
python3 --version                      # expect Python 3.14.x
apt -y install python3-venv
python3 -m venv /opt/apollonia-agent/venv
/opt/apollonia-agent/venv/bin/python --version
```
Verify on the VM: `apt-cache policy python3-venv | grep Candidate` (the venv module is a separate package on Ubuntu; on 26.04 it may be named `python3.14-venv`, so install whichever has a candidate).

Dependencies must support Python 3.14. **`audioop` no longer exists** in the standard library (removed in 3.13), so resampling and μ-law handling need a library or the lookup table (roadmap §4.2). If an important dependency has no 3.14 support, the fallback is a **uv-managed Python 3.12 inside the same venv path**, not a different OS.

---

## 9. DIDWW trunk and first tests

### 9.1 New trunk (DIDWW panel), with no production numbers on it
- [x] **New Voice IN trunk `apollonia-eleven`**, as created:
  - **Destination:** 91.99.238.181, port 5060.
  - **Transport:** **UDP**. **Network protocol:** **IPv4 only**.
  - **SRTP:** **disabled** (DIDWW ↔ Asterisk is plain RTP; only the Asterisk → OpenAI leg uses SRTP, §6.1).
  - **Request-URI user:** the default (the DID).
  - **Codecs:** **PCMA, PCMU**.
  - **Re-routing Disconnect Codes:** DIDWW's **default** set, which **excludes 401, 486, 503 and 603**. 486 and 603 must stay excluded (personal-number declines). **503 couldn't be selected when the trunk was created**: open item, see §10.
  - **Ringing Timeout** 5 s and **SIP Timer B** 6000 ms (roadmap §2.3): check in the trunk settings before the trunk group goes live (§10).
- [x] **Only `421482304191`** (the test DID) is assigned to this trunk. No trunk group yet; that comes in roadmap Phase 4.
- [x] The existing OpenAI trunk and the call assistant's trunks were **not edited**.

### 9.2 Does the INVITE arrive?
```bash
docker exec -it apollonia-pbx asterisk -rvvv
# in the Asterisk console:
pjsip set logger on
```
Call `421482304191` from a mobile phone.

**Expected:** an INVITE from a DIDWW IP, matched to endpoint `didww-eleven`, and the dialplan `from-didww-eleven` running. With no agent yet, the `CURL` comes back empty → fallback.

**Result (2026-10-19):** INVITE from **DIDWW Frankfurt**, matched to `didww-eleven`. Header facts:
- DIDWW sends **From and To without "+"**.
- DIDWW sends **no Diversion header on direct calls**, so `DIV` is empty, and the fallback's `[add-diversion]` supplies the DID.

### 9.3 Fallback to OpenAI (works before the agent exists)
For OpenAI to answer for the right tenant, the test DID belongs to the **test tenant** "Apollonia Voice Test" (not a customer), with `voice_engine='realtime'`.

**Expected:**
- Asterisk dials `openai-sip` over TLS with SRTP; OpenAI answers; Apollonia speaks with `marin`; both directions have audio;
- in the Render logs, the router's `Inbound SIP call` line shows `diversion=421482304191` and the test tenant is found.

**Result (2026-10-19): passed.** Answered by OpenAI, `marin`, two-way audio, tenant found from Diversion. Render's router accepts the DID **without "+"** in Diversion (it compares the last 9 digits).

**If something's wrong:**

| Symptom | Fix |
|---|---|
| No audio from OpenAI, or the INVITE is rejected for media | check `media_encryption=sdes` on `openai-sip` (§6.1), `pjsip reload`, call again |
| TLS: **"server identity does not match"** | `allow_wildcard_certs=yes` on `transport-tls` (keep `verify_server=yes`). OpenAI's cert is CN=api.openai.com with SAN `*.api.openai.com` (§6.1) |
| Other TLS handshake errors | check `ca-certificates` in the image and `ca_list_file` |
| OpenAI / Render answer **404** (tenant not found) | check that a Diversion header is actually sent (`pjsip set logger on`). If it's missing, the `;` in `[add-diversion]` isn't escaped as `\;` (§6.3). If it's present, compare its format with a direct DIDWW → OpenAI call's log line |

### 9.4 Personal-number decline on the fallback path
Put the test phone's number on the test tenant's personal-numbers list and call again.

**Expected:** the call is **not answered**; the caller hears busy; Asterisk shows `DIALSTATUS=BUSY` → `Hangup(17)`. Remove the number from the list afterwards.

**Result (2026-10-19): passed.** Personal number → OpenAI answered **486** → `Hangup(17)` → **486 to DIDWW**; the caller heard busy.

---

## 10. Later (not part of this runbook's first pass)
- **Open item (before the trunk group goes live, roadmap Phase 4): DIDWW re-routing on 503.**
  - **Problem:** the dialplan ends a failed fallback with `Hangup(34)` → **503**, expecting DIDWW to re-route to the OpenAI trunk. But 503 is **not** in DIDWW's default Re-routing Disconnect Codes (the default excludes 401, 486, 503, 603), and it **couldn't be set when the trunk was created**.
  - **Fix, either:**
    1. **select 503** on `apollonia-eleven`: try the trunk edit page, the trunk group settings, or ask DIDWW support; or
    2. **change `Hangup(34)`** in `extensions.conf` to a hangup cause that maps to a SIP code DIDWW re-routes **by default** (never 486/603), in the repo first, then on the VM.
  - **Then retest:** roadmap Phase 4 test 3 (fallback fails → DIDWW re-routes to the OpenAI trunk), and test 4 (a decline still doesn't re-route).
  - Also confirm the **Ringing Timeout (5 s)** and **Timer B (6000 ms)** on the trunk.
- **Agent install** (roadmap §4.2): code into `/opt/apollonia-agent`, venv, `systemctl enable --now apollonia-agent`, then the full roadmap Phase 2 exit test.
- **Phase 3:** turn on **Hetzner automatic backups** for this VM (or snapshot before every Asterisk change from then on).
- **Phase 4:** DIDWW **trunk group** (this trunk first, the OpenAI trunk second), and the failover tests.
- **Monitoring:** uptime check on the agent's `/healthz` once the agent runs (roadmap §2.7).

---

## Checklist

- [x] §2 VM + cloud firewall (2026-10-19)
- [x] §3 base system, SSH, ufw, Docker
- [x] §4 Asterisk image `apollonia-pbx:22` built (Debian trixie, Asterisk 22.11.0, `func_curl`, AudioSocket, SRTP, non-root UID 10002)
- [x] §5–7 config (mounted `:ro`), container running, verification passed, fail2ban (`sshd` + `asterisk`)
- [x] §8 agent user, env file (`600`), keys in place, unit installed (not enabled)
- [x] §9 trunk `apollonia-eleven` on test DID 421482304191; INVITE seen; fallback to OpenAI works; decline on fallback gives busy
- [ ] §10 open item: DIDWW re-routing on 503 (before the trunk group goes live)

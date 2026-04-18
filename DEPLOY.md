# Deploy to a Hetzner Cloud VPS

End-to-end: a fresh Hetzner box → the monitor running 24/7 in Docker, surviving
reboots and auto-restarting on crashes. Takes ~15 minutes.

The same instructions work on any other Ubuntu 22.04/24.04 VPS — only step 1
is Hetzner-specific.

---

## 1. Create the VPS

1. Sign up at https://console.hetzner.cloud/ and create a new project.
2. Add your SSH public key (Security → SSH Keys). If you need to generate one:
   ```bash
   ssh-keygen -t ed25519 -C "you@example.com"
   # paste ~/.ssh/id_ed25519.pub into Hetzner
   ```
3. Create a server:
   - **Image:** Ubuntu 24.04
   - **Type:** CX22 (2 vCPU / 4 GB / 40 GB / 20 TB traffic, ~€4.51/mo)
   - **Location:** any EU location (Nuremberg is typical)
   - **SSH keys:** select the one you just added
   - **Name:** `worldreveal`
4. Note the public IPv4 address it gets.

## 2. First SSH & hardening (one-time, ~3 min)

```bash
ssh root@<server-ip>

# Create a non-root user you'll use from now on.
adduser deploy
usermod -aG sudo deploy
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy

# Lock down sshd: no passwords, no root login.
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

# Basic firewall.
ufw allow OpenSSH
ufw --force enable

exit
```

From now on: `ssh deploy@<server-ip>`.

## 3. Install Docker (one-time)

```bash
ssh deploy@<server-ip>

# Official Docker install script. Safe on Ubuntu.
curl -fsSL https://get.docker.com | sudo sh

# Let your user run docker without sudo.
sudo usermod -aG docker "$USER"
newgrp docker  # or log out/in

docker --version
docker compose version
```

## 4. Clone the repo & bring your config over

```bash
# On the server:
git clone https://github.com/JacobBeaulier/WorldRevealDownloader.git
cd WorldRevealDownloader
git checkout deploy/hetzner
```

Back on your Mac, push `config.yaml` and the OAuth artifacts over. The easiest
way is to run the interactive OAuth flow **once on your laptop** (so it can open
a browser), then copy the resulting token to the server. The refresh token will
keep the server signed in indefinitely.

```bash
# On your Mac, in the project directory:
worldreveal --auth-only   # completes the browser flow, writes credentials/token.json

# Copy the config + OAuth artifacts up.
scp config.yaml deploy@<server-ip>:~/WorldRevealDownloader/config.yaml
scp credentials/oauth_client.json credentials/token.json \
    deploy@<server-ip>:~/WorldRevealDownloader/credentials/
```

### 4a. YouTube cookies (required on Hetzner / datacenter IPs)

YouTube bot-challenges anonymous requests coming from datacenter IP ranges
("Sign in to confirm you're not a bot"). yt-dlp sails through when it can send
cookies from a logged-in session. Strongly recommend using a **throwaway Google
account** so if YouTube ever flags the cookies, your main account isn't touched.

Export cookies from a browser where you're signed into YouTube as that
throwaway account:

**Option A — Chrome/Edge/Brave extension (easiest):** install [Get cookies.txt
LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc).
Visit https://www.youtube.com while signed in, click the extension, "Export"
→ "youtube.com". Save it as `youtube_cookies.txt`.

**Option B — yt-dlp on your Mac:**

```bash
# Quit Chrome first (the cookie DB must not be in use).
yt-dlp --cookies-from-browser chrome --cookies youtube_cookies.txt \
       --skip-download "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

Then ship the cookies file up and point config.yaml at it:

```bash
scp youtube_cookies.txt deploy@<server-ip>:~/WorldRevealDownloader/credentials/
```

Add this to `config.yaml` (on the server, or on the Mac before scp'ing):

```yaml
cookies_file: "./credentials/youtube_cookies.txt"
```

Cookies live for weeks to months **on a residential IP**. On a datacenter IP
(like Hetzner) YouTube often invalidates them within hours and you're back in
the bot-challenge loop. If you hit that, jump to section 4b.

### 4b. Residential proxy (durable fix for datacenter IPs)

If you're re-exporting cookies constantly, YouTube is flagging the Hetzner IP
range and cookies alone won't hold. The durable fix is routing yt-dlp's
requests through a **residential-IP proxy** — YouTube sees the traffic coming
from someone's home ISP and stops challenging it. Drive and Sheets calls
bypass the proxy (they're unaffected by any of this).

Bandwidth is the cost driver: a 500 MB video download = 500 MB of proxy
traffic. For a few dozen videos/day, budget a few GB/day.

| Provider | Starting cost | Notes |
| --- | --- | --- |
| [Webshare](https://www.webshare.io/) | Free tier: 1 GB/mo, ~$3/mo for 10 GB | Simplest signup; pick "Rotating residential". |
| [IPRoyal](https://iproyal.com/residential-proxies/) | ~$1.75/GB pay-as-you-go | No subscription; pay only for what you use. |
| [Smartproxy](https://smartproxy.com/) | ~$7/GB | Slightly pricier, very reliable. |

Pick any of the above, create an account, and get a proxy endpoint in the
form:

```
http://<user>:<pass>@<host>:<port>
```

Add this to `config.yaml`:

```yaml
proxy: "http://jacob-rotating:hunter2@p.webshare.io:80"
```

Then on the server:

```bash
cd ~/WorldRevealDownloader
git pull
docker compose up -d --build
docker compose logs -f | head -20
```

You'll see:

```
INFO worldreveal.monitor | yt-dlp proxy: http://***@p.webshare.io:80
```

confirming the proxy loaded. Once this is set up, cookies become optional —
you can leave `cookies_file` set (belt and braces) or remove it.

## 5. Start the service

```bash
# On the server:
cd ~/WorldRevealDownloader

# Pre-create the bind-mount dirs as the deploy user (UID 1000). The container
# runs as UID 1000; if these don't exist when `docker compose up` runs, the
# Docker daemon creates them as root:root and the container can't write.
mkdir -p logs credentials downloads
sudo chown -R 1000:1000 logs credentials downloads

docker compose up -d --build
docker compose logs -f       # Ctrl-C to detach; service keeps running
```

That's it. The monitor is running under Docker with `restart: unless-stopped`,
so it comes back automatically after a reboot, crash, or `docker system prune`.

## Day-to-day ops

| Task | Command |
| --- | --- |
| Tail logs | `docker compose logs -f` |
| Restart | `docker compose restart` |
| Stop  | `docker compose down` |
| Pull updates | `git pull && docker compose up -d --build` |
| Shell into the container | `docker compose exec worldreveal bash` |
| Disk usage | `df -h && docker system df` |
| Reclaim disk | `docker system prune -f` |

Host-side logs are also persisted to `./logs/worldreveal.log` (rotated 10 MB × 5).

## If the OAuth token ever expires for good

Google refresh tokens last indefinitely for "Testing" apps as long as you sign
in at least every 6 months. If yours does expire:

```bash
# On your Mac:
rm credentials/token.json
worldreveal --auth-only
scp credentials/token.json deploy@<server-ip>:~/WorldRevealDownloader/credentials/
ssh deploy@<server-ip> 'cd WorldRevealDownloader && docker compose restart'
```

## Sizing guidance

CX22 (€4.51/mo) is enough for ~dozens of videos/day with transcoding. If you
start queuing tall 4K AV1 reveals and want faster transcodes, jump to **CPX21**
(3 vCPU AMD, €7.05/mo) or **CCX13** (2 dedicated vCPU, €13.49/mo). Bandwidth is
rarely the bottleneck — 20 TB/mo covers several thousand 1080p uploads.

## Troubleshooting

- **`PermissionError: [Errno 13] Permission denied: '/app/logs/worldreveal.log'`**
  or similar in `credentials/` — the bind-mounted dir on the host is owned by
  root (Docker auto-created it because it didn't exist). Fix:
  ```bash
  docker compose down
  mkdir -p logs credentials downloads
  sudo chown -R 1000:1000 logs credentials downloads
  docker compose up -d
  ```
- **`ffmpeg not found`** — the image bakes ffmpeg in. If you see this, you're
  running against an old image: `docker compose build --no-cache && docker compose up -d`.
- **Invalid grant / token expired** — see the OAuth refresh section above.
- **`Sign in to confirm you're not a bot`** from yt-dlp — YouTube is challenging
  the datacenter IP. Short-term: re-export cookies (section 4a). Durable fix:
  add a residential proxy (section 4b) — stops the cycle.
- **Time skew** warnings from Google — `sudo timedatectl set-ntp true`.

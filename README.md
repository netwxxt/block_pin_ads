# Pinterest Ad Blocker (mitmproxy + Tailscale)

Strips promoted pins from Pinterest's iOS app/website by intercepting and modifying API responses in transit. Runs as a systemd service in an LXC container, reached from an iPhone via Tailscale exit-node routing.

## How it works

Pinterest has no separate ad domain — ads and organic pins both come from `api.pinterest.com`/`pinimg.com`, so DNS blocking (Pi-hole style) can't work. Instead, `mitmproxy` transparently intercepts HTTPS traffic, and its addon (`strip_ads.py`) walks every JSON response, deleting any pin object with an ad-indicator field (`is_promoted`, `advertiserId`, etc., in either snake_case or camelCase) before it reaches the app.

Traffic path: `iPhone --(Tailscale exit node)--> LXC (mitmproxy) --> Pinterest`, scrubbed on the way back.

## Architecture

```
/opt/mitm-venv/                    Python venv with mitmproxy
/opt/pinterest-adblock/strip_ads.py   The addon (edit AD_INDICATOR_KEYS / AD_TOKENS to update detection)
/etc/systemd/system/pinterest-adblock.service   Runs mitmdump as the `mitmproxy` user
```

Runs as a dedicated non-root user (`mitmproxy`). Always manage via `systemctl`, not by hand-running `mitmdump`

## One-time setup

**1. LXC prerequisites**
- Debian/Ubuntu LXC (unprivileged fine), Tailscale installed and `tailscale up`.
- Enable IP forwarding:
  ```bash
  echo 'net.ipv4.ip_forward=1' | sudo tee -a /etc/sysctl.conf
  echo 'net.ipv6.conf.all.forwarding=1' | sudo tee -a /etc/sysctl.conf
  sudo sysctl -p
  ```
  If it won't stick in an unprivileged LXC, set `lxc.sysctl.net.ipv4.ip_forward = 1` in the container's Proxmox config on the host, then `pct restart <id>`.
- Advertise as exit node, then approve in the [Tailscale admin console](https://login.tailscale.com/admin/machines):
  ```bash
  sudo tailscale up --advertise-exit-node
  ```

**2. Python environment**
```bash
sudo useradd -r -s /usr/sbin/nologin -d /opt/mitm-venv mitmproxy
sudo mkdir -p /opt/mitm-venv /opt/pinterest-adblock
sudo chown -R mitmproxy:mitmproxy /opt/mitm-venv /opt/pinterest-adblock
sudo -u mitmproxy python3 -m venv /opt/mitm-venv
sudo -u mitmproxy /opt/mitm-venv/bin/pip install mitmproxy
```

**3. Deploy the addon**
```bash
sudo chown mitmproxy:mitmproxy /opt/pinterest-adblock/strip_ads.py
```

**4. systemd service** — `/etc/systemd/system/pinterest-adblock.service`:
```ini
[Unit]
Description=mitmproxy Pinterest ad stripper
After=network.target tailscaled.service

[Service]
Type=simple
ExecStart=/opt/mitm-venv/bin/mitmdump --mode transparent -s /opt/pinterest-adblock/strip_ads.py --listen-host 0.0.0.0 --listen-port 8080
Restart=on-failure
User=mitmproxy
Group=mitmproxy

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pinterest-adblock
```
Optional: add `--set ignore_hosts=".*\.pinimg\.com"` to skip TLS interception for the CDN (pure perf optimization; not required for correctness).

**5. iptables transparent redirect**
```bash
sudo apt install -y iptables-persistent
sudo iptables -t nat -A PREROUTING -i tailscale0 -p tcp --dport 80 -j REDIRECT --to-port 8080
sudo iptables -t nat -A PREROUTING -i tailscale0 -p tcp --dport 443 -j REDIRECT --to-port 8080
sudo netfilter-persistent save
```

**6. iPhone setup**
1. Install Tailscale app, sign into the same tailnet.
2. Tailscale app → **Exit Node** → select this LXC.
3. With exit node active, Safari → `http://mitm.it` → download the mitmproxy CA cert.
4. **Settings → General → VPN & Device Management** → install the profile.
5. **Settings → General → About → Certificate Trust Settings** → enable full trust for the mitmproxy cert. *(Easy to miss — skipping this causes "Not Private Connection" errors.)*

Other devices will have a similar process to this, tailscale, exit node, cert, trusting cert, good-to-go.

## Day-to-day use

Nothing to do — as long as the exit node is selected and the service is running, Pinterest traffic is scrubbed automatically. Deselect the exit node to turn off.

If ads reappear right after switching the exit node on, see [Stale connections](#stale-connections-after-switching-exit-nodes) before assuming detection broke.

## Detection logic

A pin is deleted from its list if any key matches (and its value is truthy):
- `AD_INDICATOR_KEYS` — exact known field names (documented anchor list).
- `AD_TOKENS` — word roots (`promoted`, `sponsor`, `advertiser`, `campaign`, `adgroup`, `ad`) matched against each key's tokenized form (splits both snake_case and camelCase), so `is_promoted`, `isPromoted`, `advertiserId` all match on root alone. Matching is whole-token only — `added_at` doesn't match `ad`.

Every JSON response is parsed and walked unconditionally

```python
AD_INDICATOR_KEYS = {
    "is_promoted", "is_downstream_promotion", "pin_promotion_id",
    "ad_data", "advertiser_id", "sponsorship",
    "promoted_is_auto_assembled", "promoted_is_showcase",
    "promoted_is_lead_ad", "promoted_is_catalog_carousel_ad",
    "promoted_is_max_video", "promoted_quiz_pin_data",
    "ad_destination_url",
}
AD_TOKENS = {
    "ad", "ads", "promoted", "promotion", "promotions",
    "sponsor", "sponsored", "sponsorship",
    "advertiser", "advertisers", "advertising",
    "campaign", "campaigns", "adgroup",
}
```

## Maintenance

Detection is root-based, so most Pinterest field renames need no code change. If ads reappear:
1. Rule out the [stale-connection issue](#stale-connections-after-switching-exit-nodes) first — looks identical to a detection failure.
2. Capture fresh traffic to see current pin JSON (stop the service, run `mitmdump` manually as the `mitmproxy` user with `-w /tmp/capture.mitm`, then inspect with `mitmproxy -r /tmp/capture.mitm`).
3. Check logs: `sudo journalctl -u pinterest-adblock -f`
4. If a genuinely new root word appears, add it to `AD_TOKENS` (or `AD_INDICATOR_KEYS` if too generic to token-match safely — never add bare `"is"`/`"id"`).
5. `sudo systemctl restart pinterest-adblock`

## Stale connections after switching exit nodes

**Symptom:** ads reappear, or exist and are frozen right after toggling on the exit node, even though the service is fine.
**Cause:** the app's existing TCP/TLS connections keep using their old path until closed — bypassing the proxy — until a full Tailscale off/on rebuilds the tunnel.
**Fix:** after selecting the exit node, fully restart the tailscale service on your device, and reload Pinterest as well. If ads persist after that, it's a real detection issue — see Maintenance.

## Running by hand

Never run `mitmdump` with plain `sudo` — it resets `$HOME` to `/root`, causing mitmproxy to generate a brand-new untrusted CA (breaks TLS for everything). Always run as the `mitmproxy` user, matching the service's environment:
```bash
sudo -u mitmproxy /opt/mitm-venv/bin/mitmdump ...
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Not Private Connection" everywhere | Cert not fully trusted | Settings → Certificate Trust Settings |
| TLS fails for *every* domain | New untrusted CA from running `mitmdump` with plain `sudo` | See [Running by hand](#running-by-hand) |
| Ads reappear right after switching exit nodes | Stale connections | See [Stale connections](#stale-connections-after-switching-exit-nodes) |
| Service won't start | Script error or bad `ExecStart` path | `sudo journalctl -u pinterest-adblock -n 50 --no-pager` |
| No traffic in logs | Exit node not selected, or IP forwarding off | `tailscale status`; `cat /proc/sys/net/ipv4/ip_forward` |
| Ads still appear despite matches | Ad field is nested differently than expected | Capture and inspect live JSON |

## Known limitations

- Routes **all** iPhone traffic through the LXC, not just Pinterest.
- Pagination may shift slightly since ads are stripped after Pinterest already counted them into the page.
- Would break entirely if Pinterest ever added cert pinning (currently doesn't).
- Fragile to Pinterest API changes; occasional maintenance needed.

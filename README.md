# Pinterest Ad Blocker (mitmproxy + Tailscale)

Strips promoted/sponsored pins from Pinterest's iOS app and website by intercepting
and modifying API responses in transit. Runs as a systemd service inside an LXC
container, reached from an iPhone via Tailscale exit-node routing.

## How it works

Pinterest has no separate ad-serving domain — organic and promoted pins load from
the same API (`api.pinterest.com`) and CDN (`pinimg.com`), so DNS/domain-based
blocking (like Pi-hole) can't distinguish them. Instead, each pin object in the
JSON API responses carries ad-indicator fields (`is_promoted`, `pin_promotion_id`,
`ad_data`, `advertiser_id`, etc.).

This service runs `mitmproxy` as a transparent HTTPS-intercepting proxy. An addon
script (`strip_ads.py`) inspects every JSON response from Pinterest, walks the
structure recursively, and **deletes any pin object matching an ad indicator**
from its containing list — before the object ever reaches the Pinterest app to be
rendered.

Traffic path: `iPhone --(Tailscale exit node)--> LXC --(mitmproxy, transparent
mode)--> Pinterest servers`, with responses scrubbed on the way back.

## Architecture

```
/opt/mitm-venv/              Python venv (mitmproxy installed here)
/opt/pinterest-adblock/
  strip_ads.py                The addon script (edit this to update ad-field list)
/etc/systemd/system/
  pinterest-adblock.service   systemd unit running mitmdump as the `mitmproxy` user
```

Runs as a dedicated non-root system user (`mitmproxy`, no login shell) — never as
root or your personal user.

## One-time setup

### 1. LXC prerequisites
- Debian/Ubuntu LXC on Proxmox, unprivileged is fine.
- Tailscale installed and authenticated (`tailscale up`).
- IP forwarding enabled (required for exit-node routing):
  ```bash
  echo 'net.ipv4.ip_forward=1' | sudo tee -a /etc/sysctl.conf
  echo 'net.ipv6.conf.all.forwarding=1' | sudo tee -a /etc/sysctl.conf
  sudo sysctl -p
  ```
  If this doesn't stick in an unprivileged LXC, set `lxc.sysctl.net.ipv4.ip_forward = 1`
  in the container's Proxmox config on the **host**, then `pct restart <id>`.
- Advertise as Tailscale exit node, then approve it in the
  [Tailscale admin console](https://login.tailscale.com/admin/machines):
  ```bash
  sudo tailscale up --advertise-exit-node
  ```

### 2. Python environment
```bash
sudo useradd -r -s /usr/sbin/nologin -d /opt/mitm-venv mitmproxy
sudo mkdir -p /opt/mitm-venv /opt/pinterest-adblock
sudo chown -R mitmproxy:mitmproxy /opt/mitm-venv /opt/pinterest-adblock

sudo -u mitmproxy python3 -m venv /opt/mitm-venv
sudo -u mitmproxy /opt/mitm-venv/bin/pip install mitmproxy
```

### 3. Deploy the addon script
Copy `strip_ads.py` (current version below) into `/opt/pinterest-adblock/`, then:
```bash
sudo chown mitmproxy:mitmproxy /opt/pinterest-adblock/strip_ads.py
```

### 4. systemd service
`/etc/systemd/system/pinterest-adblock.service`:
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

### 5. iptables transparent redirect
```bash
sudo apt install -y iptables-persistent
sudo iptables -t nat -A PREROUTING -i tailscale0 -p tcp --dport 80 -j REDIRECT --to-port 8080
sudo iptables -t nat -A PREROUTING -i tailscale0 -p tcp --dport 443 -j REDIRECT --to-port 8080
sudo netfilter-persistent save
```

### 6. iPhone setup
1. Install the **Tailscale** app, sign in to the same tailnet.
2. In the Tailscale app: **Exit Node** → select this LXC.
3. With the exit node active, open Safari → `http://mitm.it` → download the
   mitmproxy CA cert profile.
4. **Settings → General → VPN & Device Management** → install the profile.
5. **Settings → General → About → Certificate Trust Settings** → enable full
   trust for the mitmproxy cert. *(This step is easy to miss — without it,
   sites will show "Not Private Connection" errors.)*

## Day-to-day use

Nothing to do — as long as the exit node is selected on the phone and the
systemd service is running, Pinterest traffic is scrubbed automatically.

To toggle off: deselect the exit node in the Tailscale app (reverts to normal
internet routing).

## Maintenance

**Pinterest periodically changes field names.** If ads reappear:

1. Capture fresh uBlock Origin logs on a desktop browser (Pinterest.com) to see
   current filter/scriptlet behavior, or inspect live traffic through this proxy
   directly.
2. Check the service logs for JSON responses that still contain ad content:
   ```bash
   sudo journalctl -u pinterest-adblock -f
   ```
3. Update `AD_INDICATOR_KEYS` in `strip_ads.py` with any new/renamed fields.
4. Restart:
   ```bash
   sudo systemctl restart pinterest-adblock
   ```

### Current ad-indicator fields
```python
AD_INDICATOR_KEYS = {
    "is_promoted", "is_downstream_promotion", "pin_promotion_id",
    "ad_data", "advertiser_id", "sponsorship",
    "promoted_is_auto_assembled", "promoted_is_showcase",
    "promoted_is_lead_ad", "promoted_is_catalog_carousel_ad",
    "promoted_is_max_video", "promoted_quiz_pin_data",
    "ad_destination_url",
}
```
A pin object is deleted from its containing list if **any** of these fields is
present and truthy (non-empty string/dict, `True`, or non-zero number).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "Not Private Connection" on all sites | Cert installed but not fully trusted | Settings → General → About → Certificate Trust Settings |
| Service won't start / crash loops | Script syntax error, or `ExecStart` points to wrong/missing file | `sudo journalctl -u pinterest-adblock -n 50 --no-pager` for the real traceback |
| No traffic showing in logs at all | Exit node not selected on phone, or IP forwarding disabled | `tailscale status` on phone; `cat /proc/sys/net/ipv4/ip_forward` on LXC |
| Traffic flows but nothing matches `is_pinterest()` | `pretty_host` resolving to raw IP instead of domain | Confirmed fixed via SNI fallback check — verify `strip_ads.py` includes the `is_pinterest()` SNI check |
| Ads still appear despite `MATCH — stripping` in logs | Stripping fields but not removing pin objects | Ensure using the array-filtering version (`scrub()`), not the older field-nulling version |
| Device shows offline in Tailscale after LXC restart | `tailscaled` didn't come back cleanly | `sudo systemctl restart tailscaled && sudo tailscale up --advertise-exit-node` |

### Debug logging
A verbose version of the addon (prints every request/match decision) is useful
for diagnosing new breakage. Add `print()` statements inside `response()`,
point `ExecStart` at the debug copy, and watch:
```bash
sudo journalctl -u pinterest-adblock -f
```
Revert `ExecStart` to the production script once done — debug logging adds
overhead and clutters the journal.

## Known limitations

- Exit-node routing sends **all** iPhone traffic through the LXC, not just
  Pinterest — bandwidth-light for personal use, but worth knowing.
- Pagination may shift slightly (shorter rows, earlier "load more" triggers)
  since ad objects are removed after Pinterest's server already counted them
  into the page.
- Native app cert pinning could, in principle, break this entirely if Pinterest
  ever adopts it — currently it does not, which is why interception works.
- This is fragile against Pinterest changing field names or API structure;
  expect occasional maintenance (see above).

# Pinterest Ad Blocker (mitmproxy transparent mode)

Strips promoted pins from Pinterest's iOS app by intercepting and modifying API responses. Runs as a systemd service. Devices connect via a real WireGuard server; traffic is transparently proxied through mitmproxy.

## How it works

Pinterest serves ads and organic pins from the same domains (`api.pinterest.com`, `pinimg.com`), so DNS blocking doesn't work. mitmproxy intercepts all TCP traffic from connected devices, walks every JSON response, and deletes any pin object with an ad-indicator field before it reaches the app.

Traffic path: `Device --(WireGuard)--> server (wg0) --> mitmproxy (transparent) --> internet`

## Architecture

```
/etc/wireguard/wg0.conf          WireGuard server config (peers, iptables rules)
/etc/wireguard/server_private.key
/etc/wireguard/server_public.key
/etc/wireguard/deviceN_private.key  One keypair per device
/etc/wireguard/deviceN_public.key
/opt/mitm-venv/                  Python venv with mitmproxy
/opt/mitm-venv/.mitmproxy/       mitmproxy CA cert (shared across runs)
/opt/pinterest-adblock/strip_ads.py
/etc/systemd/system/pinterest-adblock.service
```

Runs as a dedicated non-root `mitmproxy` user with `CAP_NET_ADMIN`/`CAP_NET_RAW`.

## One-time setup

**1. Prerequisites**

Debian/Ubuntu server or LXC. Install dependencies:

```bash
sudo apt install -y wireguard wireguard-tools python3 python3-venv python3-pip
```

**2. Generate WireGuard server keys**

```bash
wg genkey | sudo tee /etc/wireguard/server_private.key | wg pubkey | sudo tee /etc/wireguard/server_public.key
sudo chmod 600 /etc/wireguard/server_private.key
```

**3. Generate keys for each device**

```bash
wg genkey | sudo tee /etc/wireguard/device1_private.key | wg pubkey | sudo tee /etc/wireguard/device1_public.key
```

Repeat for each device, incrementing the number.

**4. Configure WireGuard**

Copy `wg0.conf` to `/etc/wireguard/wg0.conf` and fill in:

- `PrivateKey` — contents of `server_private.key`
- `ListenPort` — any unused UDP port
- `<LAN interface>` — your outbound interface (`ip route | grep default`, use the interface after `dev`)
- One `[Peer]` block per device with that device's public key and a unique `AllowedIPs` address

```bash
sudo chmod 600 /etc/wireguard/wg0.conf
sudo systemctl enable --now wg-quick@wg0
```

**5. Enable IP forwarding**

```bash
echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/99-forwarding.conf
sudo sysctl -p /etc/sysctl.d/99-forwarding.conf
```

**6. Python environment**

```bash
sudo useradd -r -s /usr/sbin/nologin -d /opt/mitm-venv mitmproxy
sudo mkdir -p /opt/mitm-venv /opt/pinterest-adblock
sudo chown -R mitmproxy:mitmproxy /opt/mitm-venv /opt/pinterest-adblock
sudo -u mitmproxy python3 -m venv /opt/mitm-venv
sudo -u mitmproxy /opt/mitm-venv/bin/pip install mitmproxy
```

**7. Deploy the addon**

```bash
sudo cp strip_ads.py /opt/pinterest-adblock/strip_ads.py
sudo chown mitmproxy:mitmproxy /opt/pinterest-adblock/strip_ads.py
```

**8. Generate the mitmproxy CA cert**

Run once by hand to generate the cert, then stop:

```bash
sudo -u mitmproxy /opt/mitm-venv/bin/mitmdump --mode transparent --listen-host 0.0.0.0 --listen-port 8080 --set confdir=/opt/mitm-venv/.mitmproxy
```

Ctrl+C once it prints `Transparent Proxy listening`.

**9. systemd service**

```bash
sudo cp pinterest-adblock.service /etc/systemd/system/pinterest-adblock.service
sudo systemctl daemon-reload
sudo systemctl enable --now pinterest-adblock
```

**10. DDNS (for remote access)**

Set up [DuckDNS](https://www.duckdns.org) — free, sign in with Google/GitHub, pick a subdomain. Then on the server:

```bash
mkdir -p ~/duckdns
echo 'echo url="https://www.duckdns.org/update?domains=YOURSUBDOMAIN&token=YOURTOKEN&ip=" | curl -k -o ~/duckdns/duck.log -K -' > ~/duckdns/duck.sh
chmod +x ~/duckdns/duck.sh
~/duckdns/duck.sh  # should print OK
crontab -e         # add: */5 * * * * ~/duckdns/duck.sh >/dev/null 2>&1
```

**11. Port forward**

Forward your chosen UDP port from your router's WAN to the server's LAN IP.

**12. Device setup**

On each iPhone, install the WireGuard app, create a tunnel from scratch. The app auto-generates a key pair — copy the private key, then on the server:

```bash
# paste the device's private key to derive its public key
echo "<device private key>" | wg pubkey
```

Add the public key to the matching `[Peer]` block in `wg0.conf`, then restart WireGuard:

```bash
sudo systemctl restart wg-quick@wg0
```

Configure the tunnel on the iPhone using `client.conf.example` as a template. Set `Address` to the device's assigned IP (`10.13.0.2`, `10.13.0.3`, etc.).

**13. Install the mitmproxy CA cert on each device**

With the WireGuard tunnel active:

1. Safari → `http://mitm.it` → tap Apple/iOS icon → download and install the profile
2. Settings → General → VPN & Device Management → install
3. Settings → General → About → Certificate Trust Settings → enable full trust

## Day-to-day use

Toggle the WireGuard tunnel on/off in the WireGuard app. Force-quit and reopen Pinterest after toggling on to avoid stale connections.

## Detection logic

A pin is deleted if any key matches and its value is truthy:

- `AD_INDICATOR_KEYS` — exact known field names
- `AD_TOKENS` — word roots matched against tokenized key names (`is_promoted`, `isPromoted`, `advertiserId` all match); whole-token only (`added_at` does not match `ad`)

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

If ads reappear, rule out stale connections first (force-quit Pinterest). Then:

```bash
# Capture live traffic
sudo systemctl stop pinterest-adblock
sudo -u mitmproxy /opt/mitm-venv/bin/mitmdump --mode transparent --listen-host 0.0.0.0 --listen-port 8080 --set confdir=/opt/mitm-venv/.mitmproxy -s /opt/pinterest-adblock/strip_ads.py -w /tmp/capture.mitm
# Inspect
mitmproxy -r /tmp/capture.mitm

# Check logs
sudo journalctl -u pinterest-adblock -f

# Restart after changes
sudo systemctl restart pinterest-adblock
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Feed doesn't load | mitmproxy not intercepting | Check `sudo iptables -t nat -L PREROUTING -n -v` — redirect rule must match mitmproxy's port |
| "Not Private Connection" | Cert not fully trusted | Settings → Certificate Trust Settings |
| No traffic in logs | WireGuard tunnel not connected, or wrong endpoint | Check handshake time in WireGuard app |
| Ads reappear after toggling | Stale connections | Force-quit and reopen Pinterest |
| Service won't start | Config or permissions error | `sudo journalctl -u pinterest-adblock -n 50 --no-pager` |

## Known limitations

- All device traffic routes through the server while the tunnel is active, not just Pinterest
- Pagination may shift slightly since ads are stripped after Pinterest counted them
- Breaks entirely if Pinterest adds cert pinning (currently doesn't)
- Occasional maintenance needed if Pinterest renames ad fields

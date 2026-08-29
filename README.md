# Pinterest Ad Blocker (mitmproxy + Tailscale)

Strips promoted/sponsored pins from Pinterest's iOS app and website by
intercepting and modifying API responses in transit. Runs as a systemd
service inside an LXC container, reached from an iPhone via Tailscale
exit-node routing.

## How it works

Pinterest has no separate ad-serving domain — organic and promoted pins load
from the same API (`api.pinterest.com`) and CDN (`pinimg.com`), so
DNS/domain-based blocking (Pi-hole style) can't distinguish them. Instead,
each pin object in JSON API responses carries ad-indicator fields
(`is_promoted`, `pin_promotion_id`, `advertiserId`, etc.) in either
`snake_case` (web API) or `camelCase` (mobile/GraphQL API).

`mitmproxy` runs as a transparent HTTPS-intercepting proxy. Its addon
(`strip_ads.py`) inspects every JSON response from Pinterest, walks the
structure recursively, and **deletes any pin object matching an ad
indicator** from its containing list — before the object reaches the
Pinterest app to be rendered. See [Detection logic](#detection-logic) for how
matching works.

Traffic path: `iPhone --(Tailscale exit node)--> LXC --(mitmproxy,
transparent mode)--> Pinterest servers`, scrubbed on the way back.

## Architecture

```
/opt/mitm-venv/              Python venv (mitmproxy installed here)
/opt/pinterest-adblock/
  strip_ads.py                The addon script (edit AD_INDICATOR_KEYS / AD_TOKENS to update)
/etc/systemd/system/
  pinterest-adblock.service   systemd unit running mitmdump as the `mitmproxy` user
```

Runs as a dedicated non-root system user (`mitmproxy`, no login shell) —
never as root or your personal user.

Stays Python/mitmproxy by design, not just default — see
[Design notes](#design-notes).

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
  If this doesn't stick in an unprivileged LXC, set
  `lxc.sysctl.net.ipv4.ip_forward = 1` in the container's Proxmox config on
  the **host**, then `pct restart <id>`.
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
Copy `strip_ads.py` into `/opt/pinterest-adblock/`, then:
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
   trust for the mitmproxy cert. *(Easy to miss — without it, sites show
   "Not Private Connection" errors.)*

## Day-to-day use

Nothing to do — as long as the exit node is selected on the phone and the
systemd service is running, Pinterest traffic is scrubbed automatically.

To toggle off: deselect the exit node in the Tailscale app (reverts to
normal internet routing).

## Detection logic

Two layers, both required to check off (key present **and** value truthy —
non-empty string/dict, `True`, or non-zero number):

- **`AD_INDICATOR_KEYS`** — exact known field names. Documented anchor list,
  kept even though most entries are now also caught by token matching.
- **`AD_TOKENS`** — word roots (`promoted`, `sponsor`, `advertiser`,
  `campaign`, `adgroup`, `ad`) matched against each key's *tokenized* form.
  Tokenizing splits both `snake_case` and `camelCase`, so `is_promoted`,
  `isPromoted`, and `promotedIsMaxVideo` all match on root alone — this is
  what catches `advertiserId`-style mobile/GraphQL fields without needing an
  exact entry for each casing variant.

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

Token matching is deliberately conservative: only whole tokens after
splitting on `_` and camelCase boundaries, never raw substrings. `added_at`
and `readability_score` don't match — `ad`/`ads` only counts as a standalone
token, not a substring inside a longer word.

## Maintenance

Pinterest periodically changes field names. Because detection is root-based,
most renames within a known family (e.g. new `promoted_*` variants) need no
code change. If ads reappear anyway:

1. Capture fresh uBlock Origin logs on desktop Pinterest.com, or inspect
   live traffic through this proxy directly (see
   [Debugging](#debugging-live-traffic)).
2. Watch service logs for JSON responses that still carry ad content:
   ```bash
   sudo journalctl -u pinterest-adblock -f
   ```
3. The gap is almost certainly a genuinely new root word, not a same-root
   rename — add it to `AD_TOKENS`, or add an exact field to
   `AD_INDICATOR_KEYS` if the root is too generic to token-match safely
   (don't add bare `"is"` or `"id"` — too many organic fields share those
   roots and would get wrongly deleted).
4. Restart:
   ```bash
   sudo systemctl restart pinterest-adblock
   ```

## Debugging live traffic

`journalctl` shows mitmdump's own process logs (connections, errors, addon
exceptions) — not JSON payloads, unless you add prints.

**Live / recent service logs:**
```bash
sudo journalctl -u pinterest-adblock -f
sudo journalctl -u pinterest-adblock -n 200 --no-pager
```

**Full flow inspection (request/response bodies):** run `mitmweb` manually
in place of the systemd service while debugging:
```bash
sudo /opt/mitm-venv/bin/mitmweb --mode transparent -s /opt/pinterest-adblock/strip_ads.py \
  --listen-host 0.0.0.0 --listen-port 8080 --web-host 0.0.0.0 --web-port 8081
```
Tailscale to the LXC's IP on port 8081 from a desktop browser for the web UI.
Ctrl-C to stop, then restart the real service.

**Print-debugging in the addon** — cheapest option, shows up in journalctl:
```python
def response(flow: http.HTTPFlow) -> None:
    if not is_pinterest(flow):
        return
    print(f"SEEN: {flow.request.pretty_url}")
    ...
    print(f"STRIPPED {n} pins from {flow.request.pretty_url}")
```
Point `ExecStart` at a debug copy, not the production script — revert after,
since print statements add overhead and clutter the journal.

**Flow dump to file** — for later offline replay:
```
-w /tmp/flows.mitm
```
appended to `ExecStart`, then `mitmproxy -r /tmp/flows.mitm` to browse.
Fills disk fast on a busy connection — short debug windows only.

## Design notes

**Why not Rust:** considered and rejected. `strip_ads.py` runs inside
mitmproxy's addon system, which is Python-only — the addon can't be
rewritten in Rust without replacing mitmproxy entirely. A full rewrite would
mean reimplementing cert generation, transparent-mode redirect handling,
SNI parsing, and HTTP/1.1+2 support — years of hardening, for no measurable
gain at this traffic volume (one phone's Pinterest traffic; `scrub()` walks
a few dozen pin objects per response, microseconds of work). mitmproxy's own
hot path (`mitmproxy_rs`) is already Rust under the hood — the Python layer
here was never the bottleneck. Revisit only if this starts serving many
users or CPU-bound work.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "Not Private Connection" on all sites | Cert installed but not fully trusted | Settings → General → About → Certificate Trust Settings |
| Service won't start / crash loops | Script syntax error, or `ExecStart` points to wrong/missing file | `sudo journalctl -u pinterest-adblock -n 50 --no-pager` for the real traceback |
| No traffic showing in logs at all | Exit node not selected on phone, or IP forwarding disabled | `tailscale status` on phone; `cat /proc/sys/net/ipv4/ip_forward` on LXC |
| Traffic flows but nothing matches `is_pinterest()` | `pretty_host` resolving to raw IP instead of domain | Verify `strip_ads.py` includes the SNI fallback check in `is_pinterest()` |
| Ads still appear despite matches in logs | Stripping fields but not removing pin objects | Ensure using the array-filtering version (`scrub()`), not an older field-nulling version |
| Device shows offline in Tailscale after LXC restart | `tailscaled` didn't come back cleanly | `sudo systemctl restart tailscaled && sudo tailscale up --advertise-exit-node` |

## Known limitations

- Exit-node routing sends **all** iPhone traffic through the LXC, not just
  Pinterest — bandwidth-light for personal use, but worth knowing.
- Pagination may shift slightly (shorter rows, earlier "load more" triggers)
  since ad objects are removed after Pinterest's server already counted them
  into the page.
- Native app cert pinning could, in principle, break this entirely if
  Pinterest ever adopts it — currently it does not, which is why
  interception works.
- Fragile against Pinterest changing field names or API structure; expect
  occasional maintenance (see above).

## Changelog

- **Detection rewritten to two-layer (exact + token) matching.** Previously
  exact-match only against `AD_INDICATOR_KEYS`, which missed camelCase
  fields from Pinterest's mobile/GraphQL API (`advertiserId`,
  `promotedIsMaxVideo` as camelCase, etc.). Added `AD_TOKENS` root-word
  matching against tokenized (snake_case- and camelCase-split) keys, plus a
  broader raw-text prefilter substring set before JSON parsing. Reduces how
  often `AD_INDICATOR_KEYS` needs manual updates after Pinterest renames a
  field within an already-known family.
- **Documented debugging/observability options** (mitmweb, print-debugging,
  flow dump to file) — previously only the "add print statements" note
  existed.
- **Evaluated and rejected a Rust rewrite** — addon API is Python-only, and
  a full mitmproxy replacement isn't justified at this traffic volume. See
  [Design notes](#design-notes).
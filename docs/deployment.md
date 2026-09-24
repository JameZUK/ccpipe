# Deployment

ccpipe runs HTTP only — it does not terminate TLS itself. The
recommended deployment is **nginx (or Caddy) in front, terminating
TLS**, with ccpipe's uvicorn bound to `0.0.0.0:8080` so the proxy can
reach it.

The bundled `nginx/ccpipe.conf` is a complete, production-shaped sample
(HTTPS server block + HTTP-to-HTTPS redirect + WS tuning + defence-in-
depth headers). Three pieces work together; all three must agree on
which host is the proxy:

1. **nginx** — `server_name`, cert paths, and `proxy_pass` target
2. **ccpipe backend** — runs with `--proxy-headers
   --forwarded-allow-ips=<nginx-host-IP>` and `CCPIPE_BEHIND_TLS=1`
3. **firewall** — `:8080` reachable only from the nginx host

## Topology: same-host vs off-host

| | nginx **on the same host** as ccpipe | nginx **on a different LAN host** |
|---|---|---|
| `proxy_pass` | `http://127.0.0.1:8080` | `http://<ccpipe-host>:8080` |
| ccpipe `--host` | tighten to `127.0.0.1` if you want | leave `0.0.0.0` |
| `--forwarded-allow-ips` | `127.0.0.1` | the nginx host's LAN IP |
| Firewall on `:8080` | deny all external (loopback only) | allow from nginx host only |

Off-host is the documented default; the systemd unit ships with
`--host 0.0.0.0` so it works out of the box for that case.

## Step 1: install the nginx config

```bash
# Edit the four marked spots (server_name, cert paths, proxy_pass)
sudo cp nginx/ccpipe.conf /etc/nginx/sites-available/ccpipe
sudo $EDITOR /etc/nginx/sites-available/ccpipe
sudo ln -sf /etc/nginx/sites-available/ccpipe /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## Step 2: wire the ccpipe TLS drop-in

Replace `10.0.0.5` with the actual IP of your nginx host. **The
allow-ips list MUST be tight** — every IP in it can spoof
`X-Forwarded-For` to bypass the per-IP login throttle.

```bash
mkdir -p ~/.config/systemd/user/ccpipe.service.d/
cat > ~/.config/systemd/user/ccpipe.service.d/tls.conf <<'EOF'
[Service]
Environment=CCPIPE_BEHIND_TLS=1
Environment=CCPIPE_TRUSTED_HOSTS=ccpipe.example.com
Environment=CCPIPE_ALLOWED_ORIGINS=https://ccpipe.example.com

# Reset ExecStart and re-run uvicorn with proxy-headers honoured.
# Replace the path below if you installed somewhere other than the
# recommended ~/.local/share/ccpipe (the install script bakes the
# right path into the base unit; this drop-in is just adding the
# proxy-headers flag).
ExecStart=
ExecStart=%h/.local/share/ccpipe/backend/.venv/bin/uvicorn ccpipe.main:app \
    --host 0.0.0.0 --port 8080 \
    --proxy-headers --forwarded-allow-ips=10.0.0.5 \
    --timeout-keep-alive 5 --limit-concurrency 200
EOF
systemctl --user daemon-reload && systemctl --user restart ccpipe
```

What `CCPIPE_BEHIND_TLS=1` flips on:
- Session cookie gets `Secure` + the `__Host-` prefix so it refuses to
  travel over plain HTTP.
- `TrustedHostMiddleware` binds to your hostname so HTTP `Host` header
  spoofing is rejected. **`CCPIPE_TRUSTED_HOSTS` is now required here:**
  if it's unset or `*`, ccpipe refuses to start rather than silently
  accepting any `Host` (set it as in the drop-in above; use
  `CCPIPE_ALLOW_WILDCARD_HOST=1` only if you really want the wildcard).
- WebSocket Origin checks restricted to the HTTPS origin so a page
  loaded over HTTP can't hijack the WS upgrade.
- `Strict-Transport-Security` is sent on every response.
- A startup banner reminds you to firewall `:8080` to the proxy IP.

What `--proxy-headers --forwarded-allow-ips=…` flips on:
- `request.client.host` (used by the per-IP login throttle) reads
  `X-Forwarded-For` from the proxy instead of always showing nginx's
  IP.
- The throttle log line (`login throttle tripped for ip=…`) shows the
  real client IP.
- Combined with the firewall rule below, makes the per-IP cap
  meaningful per-real-client.

## Step 3: firewall :8080 to the proxy

If nginx is off-host, only the nginx host should be allowed to reach
ccpipe's backend port. Otherwise a LAN attacker can hit `:8080` directly
over plaintext HTTP, bypassing both TLS and (in the spoofable case) the
per-IP throttle.

**The bind address is not the protection.** `--host 0.0.0.0` listens
on every IPv4 address the host has; `--host ::` listens on every IPv4
*and* IPv6 address — including a globally routable IPv6 address, which
on many home and cloud networks is reachable from the internet with no
NAT in the way. Whatever you bind, it's the firewall (or your router's
inbound IPv6 filtering) that keeps `:8080` private. Check what's
actually listening with `ss -tlnp | grep 8080`.

### ufw

ufw evaluates rules **first match wins, in insertion order**, so the
`allow` for the proxy must come *before* any `deny` on 8080 — a deny
added first would also block the proxy (the classic symptom is a
`502` from nginx).

```bash
# Replace 10.0.0.5 (and the v6 address) with the nginx host's addresses.
sudo ufw allow from 10.0.0.5 to any port 8080 proto tcp
sudo ufw allow from 2001:db8::5 to any port 8080 proto tcp   # only if nginx reaches ccpipe over IPv6
sudo ufw deny 8080/tcp                                       # everyone else, v4 and v6
sudo ufw status numbered                                     # allow lines must be listed above the deny
```

If a `deny … 8080` rule already exists, don't append the allow after
it — put it in front: `sudo ufw insert 1 allow from 10.0.0.5 to any
port 8080 proto tcp` (or `ufw delete` the deny and re-add it last).
With ufw's default `deny (incoming)` policy the explicit deny is
redundant but harmless; the allow is what matters.

ufw only applies rules to IPv6 when `IPV6=yes` in `/etc/default/ufw`
(the default on current Ubuntu/Debian — check it). With `IPV6=no`,
ufw leaves IPv6 wide open, and a `--host ::` bind is then reachable
over any public IPv6 address. A rule written without a source
(`deny 8080/tcp`) is added for both families; a rule with a source
address applies only to that address's family, so an IPv6-connected
proxy needs its own v6 `allow` line as above.

Same-host nginx (`proxy_pass http://127.0.0.1:8080`) needs no allow
rule: ufw passes loopback traffic by default. Just `sudo ufw deny
8080/tcp` — or bind `--host 127.0.0.1` and skip the firewall rule.

### nftables

Equivalent standalone table: allow tcp/8080 from loopback and the
proxy's IPv4 + IPv6 addresses, drop everything else. The `inet` family
covers both IPv4 and IPv6 in one table.

```nft
# /etc/nftables.d/ccpipe.nft (include it from /etc/nftables.conf)
table inet ccpipe {
    chain input {
        type filter hook input priority filter - 1; policy accept;

        tcp dport 8080 iifname "lo" accept
        tcp dport 8080 ip  saddr 10.0.0.5    accept   # nginx host, IPv4
        tcp dport 8080 ip6 saddr 2001:db8::5 accept   # nginx host, IPv6 (delete if unused)
        tcp dport 8080 drop
    }
}
```

Load with `sudo nft -f /etc/nftables.d/ccpipe.nft`, check with `sudo
nft list table inet ccpipe`. The chain's policy is `accept` so it
only ever affects port 8080 and leaves the rest of your ruleset alone;
the priority runs it just ahead of a standard `filter` chain. (If you
already run a default-drop `inet filter` input chain, put the three
accept lines there instead.)

Your router ACL or binding to one specific LAN address (`--host
192.168.1.50`) are also fine — but a specific-address bind still
exposes the port to everything on that LAN, so pair it with one of the
rules above when nginx is off-host.

## Optional: Cloudflare in front of nginx

If the hostname is proxied through Cloudflare (orange cloud), nginx's
TCP peer is a Cloudflare edge server, not the visitor. Out of the box
the sample config forwards that edge IP (`X-Real-IP $remote_addr`,
and it's the entry nginx appends to `X-Forwarded-For`), so ccpipe's
per-IP login throttle keys on **Cloudflare's** addresses:

- unrelated visitors share a handful of edge-IP buckets, so a
  stranger's (or a bot's) failed logins can lock **you** out for a
  minute at a time, and
- an attacker's own attempts are spread over whichever edges they hit,
  and the log lines name Cloudflare instead of them.

Fix it at nginx with the `realip` module: the commented "Cloudflare"
block in `nginx/ccpipe.conf` adds `set_real_ip_from` for each
Cloudflare range plus `real_ip_header CF-Connecting-IP;`. For requests
arriving from a listed range, nginx then replaces `$remote_addr` with
the visitor's IP, and everything downstream — `X-Real-IP`,
`X-Forwarded-For`, uvicorn's `--proxy-headers`, the throttle, the log
line — sees the real client. The ccpipe drop-in doesn't change:
`--forwarded-allow-ips` still names only the nginx host.

**`set_real_ip_from` must list only Cloudflare's ranges.** Every
address in it is trusted to name the client, so adding anything else
(your LAN, `0.0.0.0/0`, a stale range Cloudflare no longer owns) lets
whoever connects from there send `CF-Connecting-IP: <any IP>` and pick
a fresh throttle bucket per attempt. Take the ranges from
<https://www.cloudflare.com/ips/> (plain lists at `/ips-v4` and
`/ips-v6`); the copy in the sample is a snapshot and will drift, so
re-check it periodically. Ideally also firewall `:443` so only
Cloudflare's ranges can reach nginx at all — that stops anyone
bypassing Cloudflare by connecting to the origin directly.

Security headers: Cloudflare can add its own (HSTS in particular);
ccpipe already sends them, and for HSTS the browser honours the first
header it sees, which is the backend's — see the note in the nginx
sample.

## Verifying the wiring

After `systemctl --user restart ccpipe` and `systemctl reload nginx`:

```bash
# 1. ccpipe's startup banner should warn about :8080:
journalctl --user -u ccpipe -b | grep -A 6 BEHIND_TLS

# 2. Hit the site over HTTPS — should return JSON, not an error:
curl -sS https://ccpipe.example.com/api/health

# 3. Trip the login throttle (5 attempts/min per IP; the 6th is refused
#    and logged) and check which IP the journal names. This locks your
#    own IP out of logging in for a minute.
for i in 1 2 3 4 5 6; do
  curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
       https://ccpipe.example.com/api/auth/login \
       -H 'Content-Type: application/json' \
       -H 'X-Requested-By: ccpipe' \
       -d '{"username":"bad","password":"bad"}'
done          # expect 401 ×5, then 429
journalctl --user -u ccpipe --since -2min | grep "login throttle"
# If the logged IP is the nginx host, --proxy-headers isn't wired.
# If it's a Cloudflare address, see "Cloudflare in front of nginx".
# If it's your laptop's public IP, you're good.

# 4. Backend should NOT be reachable directly from anywhere except
#    the nginx host:
curl -sS --max-time 3 http://<ccpipe-host>:8080/api/health
# expected: timeout from a LAN host that isn't the proxy
```

## Common gotchas

- **WebSocket disconnects after ~60 s of idle.** `proxy_read_timeout`
  defaults to 60 s on nginx. The sample sets it to 1 day; tune shorter
  if you want.
- **Login throttle locks out everyone after 5 attempts.** Without
  `--proxy-headers --forwarded-allow-ips=<nginx-IP>` every request shows
  up as the same source IP and the per-IP cap is effectively global.
  Fix with the drop-in above. Behind Cloudflare the same thing happens
  with edge IPs — see "Cloudflare in front of nginx".
- **Cookie not set on first login.** `__Host-` cookies require both
  `Secure` and `Path=/`; if you forgot `CCPIPE_BEHIND_TLS=1`, the Secure
  flag isn't applied and the browser silently drops the cookie under
  HTTPS. Look for `Set-Cookie: __Host-ccpipe_session` in the response
  headers to confirm.
- **`502 Bad Gateway` on first request.** Either nginx is pointing at
  the wrong `proxy_pass` host/port, or the firewall rule blocks the
  nginx host. `curl http://<ccpipe-host>:8080/api/health` from the nginx
  box should return JSON.

## Caddy

Caddy can replace the nginx server block in 5 lines and handles TLS
issuance automatically:

```caddy
ccpipe.example.com {
    reverse_proxy <ccpipe-host>:8080 {
        # Trust X-Forwarded-* from Caddy. Caddy sets these by default.
        header_up Host {host}
    }
}
```

Pair with the same `tls.conf` systemd drop-in, swapping
`--forwarded-allow-ips=` for the Caddy host's IP.

## Updating an existing install

```bash
git pull
# Backend deps: pinned by backend/constraints.txt (same as install.sh).
backend/.venv/bin/pip install -q -c backend/constraints.txt -e backend
# Frontend: npm ci installs exactly what package-lock.json pins.
(cd frontend && npm ci && npm run build)
systemctl --user restart ccpipe
```

Re-running `scripts/install.sh` does the same (and also re-installs
the unit files; your `tls.conf` drop-in is left alone).

## Production troubleshooting

- **`/voice` says no audio device**: run `pactl list short sources |
  grep ccpipe_mic` — if missing, the virtual-mic service isn't loaded;
  `systemctl --user restart ccpipe-virtual-mic`. On Wayland sessions
  PulseAudio may need to be replaced by PipeWire's `pulseaudio` shim.
- **WS keeps reconnecting on mobile**: usually fine — the client treats
  the socket as stale after 45s of silence and re-dials. Check
  `journalctl --user -u ccpipe -f` for backend errors.
- **TTS silent on mobile**: tap the page once after attaching to a
  session. Browsers gate `AudioContext` resumption behind a user
  gesture. The voice pill in the statusbar should turn amber once it's
  wired.
- **Lost the generated initial password**: the credentials file stores
  only the hash. If you didn't capture the password from
  `~/.local/state/ccpipe/initial_password.txt` before deleting it,
  delete the credentials file itself
  (`rm ~/.local/state/ccpipe/credentials`) and restart ccpipe — it will
  regenerate fresh credentials and write a new sidecar.
- **Login throttle keeps you locked out**: rate-limit windows are 60 s.
  Wait, or `systemctl --user restart ccpipe` to reset the in-memory
  buckets immediately.
- **`open terminal failed: not a terminal` in logs**: used to fire
  during tmux control-client startup; suppressed in current builds. If
  you still see it, you're on an older build.

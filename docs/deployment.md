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
| Firewall on `:8080` | deny all external | allow from nginx host only |

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

```bash
# ufw example — replace 10.0.0.5 with the nginx host's IP
sudo ufw deny  in on <iface> to any port 8080
sudo ufw allow from 10.0.0.5 to any port 8080
sudo ufw reload
```

iptables, nftables, your router ACL, or binding to a specific LAN
interface (`--host 192.168.1.50`) all achieve the same thing — pick
whatever fits your environment.

## Verifying the wiring

After `systemctl --user restart ccpipe` and `systemctl reload nginx`:

```bash
# 1. ccpipe's startup banner should warn about :8080:
journalctl --user -u ccpipe -b | grep -A 6 BEHIND_TLS

# 2. Hit the site over HTTPS — should return JSON, not an error:
curl -sS https://ccpipe.example.com/api/health

# 3. Deliberately fail a login and watch the journal for the real IP:
curl -sS -X POST https://ccpipe.example.com/api/auth/login \
     -H 'Content-Type: application/json' \
     -H 'X-Requested-By: ccpipe' \
     -d '{"username":"bad","password":"bad"}' &
journalctl --user -u ccpipe -f | grep "login throttle"
# If the logged IP is the nginx host, --proxy-headers isn't wired.
# If it's your laptop, you're good.

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
  Fix with the drop-in above.
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
cd frontend && npm run build && cd ..
# (re-install backend deps only if pyproject.toml changed)
systemctl --user restart ccpipe
```

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

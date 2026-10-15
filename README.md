# scavenger

A single Docker container that looks like a plain "coming soon" web page but, driven
entirely by environment variables, hunts the internet in the background:

1. **Anonymous SOCKS5 / HTTP proxies** on cloud datacenter IPs (18 provider feeds,
   all regions) and on random residential IPv4 space — every proxy catch is
   **HTTPS-verified**: the probe opens a real `CONNECT` tunnel to port 443 and
   completes a full TLS handshake through it, so a catch actually works for
   modern TLS sites and apps. Plain-HTTP-only proxies (which can't carry
   HTTPS) are not caught.
2. **Weakly configured LLM gateways** — anything serving a `GET /models` endpoint
   (under the standard `/v1` prefix, or at the bare root in rare cases) that
   returns a model list is probed with a simple `hello` chat completion. Two weak
   auth flavors are sent: `Bearer <key>` (default key `test`, configurable) **and**
   a bare `Bearer` with an empty token, to catch gateways that accept any random or
   empty key. Both `http://` and `https://` are tried on every port, because
   self-hosted gateways are plain or TLS depending on the deployer. A `429` on the
   chat call also counts as a catch — it proves the gateway actually dispatched the
   request upstream. The catch message shows the **working** API base
   (e.g. `https://ip:port/v1`) so you can point a client at it directly.

Every catch is:

- sent to **your Telegram chat** as one message per catch (bot token + chat id via
  env, no webhook URL needed — works on any PaaS or in Portainer),
- appended to a **local JSONL file** (`DATA_DIR/catches.jsonl`, survives restarts,
  deduped by type + ip + port + model),
- re-checkable at any time with `/revalidate` (dead entries are removed from the file).

On graceful shutdown (SIGTERM, i.e. a PaaS redeploy) the full catch list is backed up
to Telegram **if a chat is bound**, before the container exits.

## Contents

- [How it works](#how-it-works)
- [Deploy](#deploy) — [GHCR image](#1-ghcr-image-recommended), [local build](#2-build-locally),
  [PaaS](#3-paas-render--railway--fly), [Portainer](#4-portainer)
- [Telegram commands](#telegram-commands)
- [Environment variables](#environment-variables)
- [Self test](#self-test)

## How it works

### Scanner groups

Two independent scanner groups run forever, each with `N` worker loops
(`CLOUD_SCANNERS` / `RESIDENTIAL_SCANNERS`, default 1 each):

- **Cloud group** — pulls official IP-range feeds at start and every
  `FEED_REFRESH_HOURS` (default 6h). Where a provider publishes a
  machine-readable list it is used directly; the rest use their announced
  prefixes from RIPEstat per ASN (a single dead ASN never drops the whole
  provider). Ranges are expanded into /24 blocks, shuffled, and round-robined
  across providers forever; each block contributes `SAMPLE_PER_24` random IPs
  (default 2), each probed on every `SCAN_PORTS` entry.

  | provider     | feed source                                          |
  |--------------|------------------------------------------------------|
  | aws          | `ip-ranges.amazonaws.com/ip-ranges.json`             |
  | azure        | ServiceTags JSON, link resolved from the Microsoft download page (filename is dated and rotates) |
  | gcp          | `gstatic.com/ipranges/cloud.json`                    |
  | oci          | `docs.cloud.oracle.com/…/public_ip_ranges.json`      |
  | cloudflare   | `api.cloudflare.com/client/v4/ips`                   |
  | fastly       | `api.fastly.com/public-ip-list`                      |
  | vultr        | RFC 8805 geofeed `geofeed.constant.com/?json`        |
  | hetzner      | RIPEstat announced prefixes, AS24940                 |
  | digitalocean | RIPEstat announced prefixes, AS14061                 |
  | alibaba      | RIPEstat announced prefixes, AS45102/23532/60924/134963 |
  | tencent      | RIPEstat announced prefixes, AS132203 + 6 related ASNs |
  | linode       | RIPEstat announced prefixes, AS63949                 |
  | ovh          | RIPEstat announced prefixes, AS16276                 |
  | scaleway     | RIPEstat announced prefixes, AS12876                 |
  | upcloud      | RIPEstat announced prefixes, AS25697 + AS202053      |
  | ionos        | RIPEstat announced prefixes, AS8560                  |
  | yandex       | RIPEstat announced prefixes, AS13238/20144/207304    |
  | ibm          | RIPEstat announced prefixes, IBM/SoftLayer AS set (67 ASNs) |

- **Residential group** — picks uniform random public IPv4 /24 blocks (RFC1918,
  link-local, multicast, test ranges skipped) with the same sampling.

### Probes

Per sampled `ip:port` (default 15 ports, including LLM-gateway ports **20128** and
**20130**), three probes run in parallel under a shared per-group concurrency limit
(`CHECK_CONCURRENCY`, default 50):

- **SOCKS5** — version-5 greeting, must accept no-auth, then a real CONNECT
  through to `api.ipify.org:443`, a TLS handshake inside the tunnel, and a
  `GET /` over TLS to capture the egress IP (shown in the alert). A catch is a
  proxy proven to carry HTTPS.
- **HTTP proxy** — `CONNECT api.ipify.org:443` must return 200, then a TLS
  handshake and a `GET /` through the tunnel must return 200 with a real IP
  body. Proxies that only do plain absolute-form GETs (no `CONNECT`) are
  skipped — they can't be used for HTTPS sites/apps.
- **LLM** — every combination of scheme (`http://ip:port`, `https://ip:port`),
  auth (`Bearer <LLM_TEST_API_KEY>`, default `test`, plus a bare `Bearer` with an
  empty token), and API base (`{base}/v1` — the normal layout, and the bare
  `{base}` root — the rare no-`/v1` gateways) is probed in parallel; the first hit
  wins and the winning scheme + working base (`api_base`) are stored with the
  catch. A hit is: `GET {base}/v1/models` (or `{base}/models`) returns a model
  list, pick the first model, then `POST …/v1/chat/completions` (or
  `…/chat/completions`) with
  `{"messages":[{"role":"user","content":"hello"}]}`, `max_tokens=LLM_MAX_TOKENS`
  (default 256 — big enough that reasoning models don't burn the whole budget
  thinking before answering). Counted as a catch:
  - 200 with a non-empty reply (`status: ok`),
  - **429** — rate-limited, i.e. the gateway dispatched the request upstream
    (`status: 429`),
  - 200 with an empty reply and `finish_reason: length` — a reasoning model that
    spent the whole budget thinking (`status: length`).

Catches are deduped by (type, ip, port, model), appended to the JSONL, logged, and
sent to Telegram immediately (failed sends and documents are buffered up to
100 entries and retried on the next poll).

## Deploy

### 1. GHCR image (recommended)

Every push to this repo builds and publishes a **multi-arch** (linux/amd64 +
linux/arm64) image to GitHub Container Registry via GitHub Actions:

```
ghcr.io/foxy1402/scavenger:latest      # always the newest commit
ghcr.io/foxy1402/scavenger:<sha>       # pinned per commit, if you want to roll back
```

No secret setup is needed — the repo is public and the workflow pushes with the
built-in `GITHUB_TOKEN`. Just pull and run:

```bash
docker pull ghcr.io/foxy1402/scavenger:latest

# quick sanity check (see Self test below)
docker run --rm -e SELFTEST=1 ghcr.io/foxy1402/scavenger:latest

# real run
docker run -d --name scavenger \
  -e TELEGRAM_BOT_TOKEN="123456:AA..." -e TELEGRAM_CHAT_ID="123456789" \
  -p 8080:8080 -v "$(pwd)/data:/data" --restart unless-stopped \
  ghcr.io/foxy1402/scavenger:latest
```

On Windows with Git Bash, use `-v "$(pwd -W)/data:/data"` instead — MSYS path
conversion mangles the `$PWD/…` form (the volume silently lands in a `data;C`
directory or an ephemeral VM path).

**First run on a host without `data/`:** Docker creates the bind-mount source
root-owned, and the container runs as uid 10001 — pre-create the directory so
catches are not lost:

```bash
mkdir -p data && sudo chown -R 10001:10001 data
```

(If the host user's uid happens to be 10001, the `chown` is a no-op.)

### 2. Build locally

```bash
docker build -t scavenger:latest .
cp .env.example .env        # put your telegram token + chat id in it
mkdir -p data && sudo chown -R 10001:10001 data
docker compose up -d
docker compose logs -f
```

The page: `http://localhost:8080/` · health: `http://localhost:8080/healthz`

`docker-compose.yml` passes through every documented env var from `.env` (including
timeouts and `PROVIDERS`) except `DATA_DIR`, which is fixed to the mounted `/data`;
the `./data` volume persists the catch file on the host.

### 3. PaaS (Render / Railway / Fly / …)

1. Build & push the image (or let the PaaS build it from this repo — it's a
   standard Dockerfile). Pulling `ghcr.io/foxy1402/scavenger:latest` works
   directly on any PaaS too.
2. Set env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. The PaaS `PORT` is
   picked up automatically.
3. Point the PaaS health check at `/healthz` (the container also ships a Docker
   `HEALTHCHECK` that hits it).

No persistent volume is needed: catches live in the container and are backed up to
Telegram on graceful shutdown (SIGTERM) **if a chat is bound** — the bot sends you
the full list (text, or a `.txt` attachment when long) before the container stops.
Redeploys therefore never lose your list.

### 4. Portainer

**Compose stack** (recommended — persists the file on the host):

```bash
scp -r scavenger user@server:~/
cd ~/scavenger && cp .env.example .env   # edit token/chat id
mkdir -p data && chown -R 10001:10001 data
docker compose up -d --build
```

or upload `docker-compose.yml` as a Portainer stack (it will build the image).

**Standalone container** (image: `ghcr.io/foxy1402/scavenger:latest` or
`scavenger:latest` after a local build):

- ports: `8080:8080` (or `${PORT}:${PORT}`)
- env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (+ anything from `.env.example`)
- volume (optional, recommended): `./data` → `/data`, pre-created and owned by uid
  10001 as above

## Telegram commands

Set `TELEGRAM_BOT_TOKEN` (+ optionally `TELEGRAM_CHAT_ID`) and, if the chat id is
unset, send `/start` to the bot to bind it. Commands work in groups too
(`@BotName` suffixes are handled):

| command       | what it does                                                          |
|---------------|-----------------------------------------------------------------------|
| `/status`     | uptime, catch counts, scanner progress, feed sizes                    |
| `/revalidate` | re-probes every catch; dead ones are removed from the file (catches recorded mid-run survive); you get a summary |
| `/export`     | full list of current catches — text if short, `.txt` attachment if long |
| `/start`      | (re)bind the bot to the chat that sent it                             |

Example catch message:

```
🤖 CATCH — LLM GATEWAY
addr     https://54.172.31.9:8000/v1
model    gpt-4o-mini
reply    Hello! How can I help you today?
where    cloud/aws
captured 2026-10-14T12:00:00Z
```

`addr` is the working API base the probe found — almost always `…:port/v1`, but a
bare `…:port` when the gateway serves its endpoints at the root. Exports use the
same base, so you can copy-paste it as the `base_url` of any OpenAI-compatible
client. 429 / reasoning-budget catches carry an extra `status` line (`429` /
`length`) and the same marker in exports. Proxy catches stay plain `ip:port` (their
usable form is `socks5://ip:port` / `http://ip:port`, as shown in exports).

## Environment variables

All optional. Defaults are conservative (1 worker per group, concurrency 50) — the
bot makes a slow, polite pass; raise the knobs for more coverage.

| var                    | default                       | meaning                                             |
|------------------------|-------------------------------|-----------------------------------------------------|
| `PORT`                 | `8080`                        | web port (PaaS sets this)                           |
| `TELEGRAM_BOT_TOKEN`   | —                             | your bot token (log-only mode without it)           |
| `TELEGRAM_CHAT_ID`     | —                             | your chat uid; `/start` binds if unset              |
| `CLOUD_SCANNERS`       | `1`                           | cloud worker loops                                  |
| `RESIDENTIAL_SCANNERS` | `1`                           | residential worker loops                            |
| `CHECK_CONCURRENCY`    | `50`                          | concurrent probes per scanner group                 |
| `SCAN_PORTS`           | 15 ports (see `.env.example`) | ports probed on every sampled IP, incl. LLM-gateway ports 20128/20130 |
| `SAMPLE_PER_24`        | `2`                           | random IPs probed inside each /24                   |
| `PROVIDERS`            | all 18 (see `.env.example`)   | cloud feeds to use                                  |
| `LLM_TEST_API_KEY`     | `test`                        | sent as `Bearer <key>` in one probe variant; a bare `Bearer` (empty token) variant is always probed too |
| `LLM_MAX_TOKENS`       | `256`                         | hello-completion budget; reasoning models may spend tokens before answering |
| `CONNECT_TIMEOUT`      | `2.5`                         | seconds                                             |
| `PROBE_TIMEOUT`        | `6`                           | seconds                                             |
| `CHAT_TIMEOUT`         | `30`                          | seconds (LLM completions can be slow)               |
| `FEED_REFRESH_HOURS`   | `6`                           | cloud IP feed refresh interval                      |
| `DATA_DIR`             | `/data`                       | where `catches.jsonl` lives                         |
| `PAGE_TITLE` / `PAGE_CONTENT` | "Coming soon" / …       | camouflage page text                                |
| `SELFTEST`             | `0`                           | `1` = run mock end-to-end self test and exit 0/1    |

## Self test

```bash
docker run --rm -e SELFTEST=1 -p 8080:8080 scavenger:latest
# ...
# INFO scavenger: SELFTEST OK 3/3
```

Starts in-process mock SOCKS5 / HTTP proxy / LLM servers, points the scanner at
`127.0.0.1`, and exits 0 only after 3 catches are recorded (one per probe type).
The web page is live on `:8080` while it runs. Works on the GHCR image too — a good
smoke test after a pull.

Offline bot/command test (probes, store dedupe, `/export`, `/revalidate`):

```bash
docker run --rm scavenger:latest python -m tests.test_commands
# one PASS line per check, ending with RESULT: PASS
```

## Project layout

```
app/main.py            asyncio wiring: web + scanners + bot + shutdown backup
app/config.py          env parsing with defaults
app/web.py             camouflage page, /healthz, /favicon.ico
app/store.py           JSONL catch store, in-memory dedupe, crash-safe replace
app/probes.py          SOCKS5 / HTTP-proxy / LLM-gateway probes (dual scheme)
app/ranges.py          18 cloud IP feeds + residential random /24 generator
app/scanners.py        scanner groups: worker loops, sampling, recording
app/telegram_bot.py    long polling, per-catch messages, /status /revalidate /export
app/selftest.py        in-process mock servers for SELFTEST mode
tests/test_commands.py offline end-to-end checks
Dockerfile             python:3.12-slim, non-root, HEALTHCHECK (multi-arch safe)
docker-compose.yml     Portainer/local stack, full env passthrough
.github/workflows/publish.yml   multi-arch GHCR publish on every push
```

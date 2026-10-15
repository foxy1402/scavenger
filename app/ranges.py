import asyncio
import ipaddress
import logging
import random
import re

import aiohttp

log = logging.getLogger("scavenger.ranges")

# feed types:
#   json  — official machine-readable list; any JSON shape works (CIDRs walked)
#   azure — dated ServiceTags file, link resolved from the download page
#   ripe  — announced prefixes per ASN via RIPEstat (partial failure tolerated)
FEEDS = {
    "aws": {"type": "json", "url": "https://ip-ranges.amazonaws.com/ip-ranges.json"},
    "azure": {"type": "azure"},
    "gcp": {"type": "json", "url": "https://www.gstatic.com/ipranges/cloud.json"},
    "oci": {"type": "json",
            "url": "https://docs.cloud.oracle.com/en-us/iaas/tools/public_ip_ranges.json"},
    "hetzner": {"type": "ripe", "asns": ["AS24940"]},
    "cloudflare": {"type": "json", "url": "https://api.cloudflare.com/client/v4/ips"},
    "digitalocean": {"type": "ripe", "asns": ["AS14061"]},
    "alibaba": {"type": "ripe",
                "asns": ["AS45102", "AS23532", "AS60924", "AS134963"]},
    "tencent": {"type": "ripe",
                "asns": ["AS132203", "AS45090", "AS58835", "AS132591",
                         "AS133478", "AS134103", "AS139341"]},
    # RFC 8805 geofeed published by The Constant Company (Vultr's operator)
    "vultr": {"type": "json", "url": "https://geofeed.constant.com/?json"},
    "linode": {"type": "ripe", "asns": ["AS63949"]},
    "fastly": {"type": "json", "url": "https://api.fastly.com/public-ip-list"},
    "ovh": {"type": "ripe", "asns": ["AS16276"]},
    "scaleway": {"type": "ripe", "asns": ["AS12876"]},
    "upcloud": {"type": "ripe", "asns": ["AS25697", "AS202053"]},
    "ionos": {"type": "ripe", "asns": ["AS8560"]},
    "yandex": {"type": "ripe", "asns": ["AS13238", "AS20144", "AS207304"]},
    # IBM/SoftLayer AS set (AS63949 excluded — counted under linode)
    "ibm": {"type": "ripe",
            "asns": ["AS4759", "AS7489", "AS11172", "AS14417", "AS16807",
                     "AS17390", "AS19281", "AS20546", "AS20675", "AS21844",
                     "AS22400", "AS22954", "AS23468", "AS25773", "AS25926",
                     "AS26695", "AS26968", "AS28249", "AS28356", "AS30315",
                     "AS31688", "AS36167", "AS36344", "AS36351", "AS38719",
                     "AS43047", "AS44588", "AS46160", "AS48851", "AS49072",
                     "AS49765", "AS50524", "AS50526", "AS53334", "AS55082",
                     "AS55789", "AS59630", "AS62874", "AS63440", "AS63912",
                     "AS64999", "AS131476", "AS131631", "AS131659",
                     "AS134176", "AS134826", "AS134829", "AS135134",
                     "AS138145", "AS198605", "AS200432", "AS200840",
                     "AS202665", "AS203734", "AS204090", "AS204508",
                     "AS204527", "AS205434", "AS205805", "AS206998",
                     "AS208162", "AS212795", "AS213242", "AS219514",
                     "AS263034", "AS393472", "AS394695"]},
}

AZURE_PAGE_URL = "https://www.microsoft.com/en-us/download/details.aspx?id=56519"
# the ServiceTags JSON filename is dated and rotates; always resolve it from the page
_AZURE_JSON_RE = re.compile(
    r"https://download\.microsoft\.com/download/[^\"' ]+ServiceTags_Public_\d{8}\.json")

_USER_AGENT = "scavenger/1.0 (cloud ip feed; 1 req/6h)"

_CIDR_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}$")
_BARE_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def extract_cidrs(node, out):
    """Recursively collect IPv4 CIDRs (or bare IPs) from any JSON shape."""
    if isinstance(node, str):
        s = node.strip()
        if _CIDR_RE.match(s):
            out.append(s)
        elif _BARE_RE.match(s):
            out.append(s + "/32")
    elif isinstance(node, dict):
        for v in node.values():
            extract_cidrs(v, out)
    elif isinstance(node, list):
        for v in node:
            extract_cidrs(v, out)


def cidrs_to_24s(cidrs):
    """Expand a set of CIDRs into a deduplicated list of /24 blocks."""
    seen = set()
    out = []
    for c in cidrs:
        try:
            net = ipaddress.ip_network(c, strict=False)
        except ValueError:
            continue
        if net.version != 4:
            continue
        if net.prefixlen < 2:
            # a /0 or /1 would expand to 8-16 million /24 blocks
            log.warning("skipping oversized prefix %s", c)
            continue
        if net.prefixlen < 24:
            subs = list(net.subnets(new_prefix=24))
        else:
            subs = [net]
        for s in subs:
            key = str(s)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


class CloudRangePool:
    """Round-robin source of (provider, /24) units from official cloud IP feeds."""

    def __init__(self, providers):
        self.providers = [p for p in providers if p in FEEDS]
        unknown = [p for p in providers if p not in FEEDS]
        if unknown:
            log.warning("unknown providers ignored: %s (known: %s)",
                        ",".join(unknown), ",".join(FEEDS))
        self.lists = {}
        self.cursor = {}
        self.order = []
        self.rr = 0
        self.ready = False
        self.lock = asyncio.Lock()

    async def refresh(self, session):
        results = await asyncio.gather(
            *(self._fetch(p, session) for p in self.providers),
            return_exceptions=True)
        new_lists = dict(self.lists)
        for p, res in zip(self.providers, results):
            if isinstance(res, Exception):
                log.warning("feed %s failed: %s", p, res)
                continue
            new_lists[p] = cidrs_to_24s(res)
            random.shuffle(new_lists[p])
            log.info("feed %s: %d /24 blocks", p, len(new_lists[p]))
        async with self.lock:
            self.lists = new_lists
            self.cursor = {p: 0 for p in new_lists}
            self.order = []
            self.ready = True
        return {p: len(l) for p, l in new_lists.items()}

    async def _fetch(self, provider, session):
        spec = FEEDS[provider]
        if spec["type"] == "azure":
            return await self._fetch_azure(session)
        if spec["type"] == "ripe":
            return await self._fetch_ripe(session, spec)
        async with session.get(spec["url"], headers={"User-Agent": _USER_AGENT},
                               timeout=aiohttp.ClientTimeout(total=120)) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
        out = []
        extract_cidrs(data, out)
        return out

    async def _fetch_ripe(self, session, spec):
        """Announced IPv4 prefixes per ASN via RIPEstat; one dead ASN is no
        reason to drop the whole provider."""
        cidrs = set()
        errors = 0
        for asn in spec["asns"]:
            try:
                async with session.get(
                        "https://stat.ripe.net/data/announced-prefixes/data.json"
                        f"?resource={asn}",
                        headers={"User-Agent": _USER_AGENT},
                        timeout=aiohttp.ClientTimeout(total=60)) as r:
                    r.raise_for_status()
                    data = await r.json(content_type=None)
                for item in (data.get("data") or {}).get("prefixes") or []:
                    prefix = item.get("prefix", "")
                    if prefix.count(":") == 0:  # IPv4 only
                        cidrs.add(prefix)
            except Exception as e:
                errors += 1
                log.debug("ripe feed %s failed: %s", asn, e)
        if not cidrs:
            raise RuntimeError(f"ripe feed: no prefixes fetched ({errors} asn errors)")
        if errors:
            log.warning("ripe feed: %d/%d asn lookups failed (partial list kept)",
                        errors, len(spec["asns"]))
        return list(cidrs)

    async def _fetch_azure(self, session):
        async with session.get(AZURE_PAGE_URL, headers={"User-Agent": _USER_AGENT},
                               timeout=aiohttp.ClientTimeout(total=60)) as r:
            r.raise_for_status()
            html = await r.text()
        m = _AZURE_JSON_RE.search(html)
        if not m:
            raise RuntimeError("azure ServiceTags json link not found on download page")
        async with session.get(m.group(0), headers={"User-Agent": _USER_AGENT},
                               timeout=aiohttp.ClientTimeout(total=300)) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
        out = []
        extract_cidrs(data, out)
        return out

    def _new_round(self):
        self.order = [p for p in self.providers if self.lists.get(p)]
        random.shuffle(self.order)
        self.cursor = {p: 0 for p in self.order}
        self.rr = 0

    async def next_unit(self):
        while True:
            async with self.lock:
                if not self.ready:
                    return None, None
                while True:
                    if not self.order:
                        self._new_round()
                        if not self.order:
                            return None, None
                    p = self.order[self.rr % len(self.order)]
                    self.rr += 1
                    lst = self.lists.get(p)
                    if not lst:
                        self.order.remove(p)
                        continue
                    i = self.cursor[p]
                    cidr = lst[i]
                    self.cursor[p] = i + 1
                    if self.cursor[p] >= len(lst):
                        self.order.remove(p)
                    return p, cidr


_EXCLUDE = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
)]


def _random_residential_24():
    while True:
        val = random.getrandbits(32)
        # tuple form: a bare int with a second positional arg would set `strict`,
        # not the prefixlen, and silently yield a /32
        net = ipaddress.ip_network((val & 0xFFFF_FF00, 24))
        if any(net.subnet_of(e) for e in _EXCLUDE):
            continue
        return str(net)


class ResidentialPool:
    """Endless source of random public /24 blocks (residential-ish)."""

    async def next_unit(self):
        return None, _random_residential_24()

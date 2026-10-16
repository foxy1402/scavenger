import logging
import os

log = logging.getLogger("scavenger.config")


def _int(name, default):
    raw = os.environ.get(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        log.warning("%s=%r is not an integer — using %s", name, raw, default)
        return default


def _float(name, default):
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw.strip() else default
    except ValueError:
        log.warning("%s=%r is not a number — using %s", name, raw, default)
        return default


def _bool(name, default=False):
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Config:
    # 20128/20130: default ports of two popular self-hosted LLM gateways;
    # 7860: Gradio (common front for self-hosted models); 3000/1234/1337/
    # 30000: dev servers and custom LLM/proxy ports seen in the wild
    DEFAULT_PORTS = ("80,443,8000,8080,3128,8888,1080,1081,10800,11434,"
                     "4000,5000,9000,20128,20130,1234,30000,1337,7860,3000")
    DEFAULT_PROVIDERS = ("aws,azure,gcp,oci,hetzner,cloudflare,digitalocean,"
                         "alibaba,tencent,vultr,linode,fastly,ovh,scaleway,"
                         "upcloud,ionos,yandex,ibm,contabo,leaseweb")

    def __init__(self):
        self.port = _int("PORT", 8080)
        if not 1 <= self.port <= 65535:
            log.warning("PORT=%d out of range — using 8080", self.port)
            self.port = 8080
        self.cloud_scanners = max(1, _int("CLOUD_SCANNERS", 1))
        self.residential_scanners = max(1, _int("RESIDENTIAL_SCANNERS", 1))
        self.check_concurrency = max(1, _int("CHECK_CONCURRENCY", 50))
        self.scan_ports = self._ports(os.environ.get("SCAN_PORTS", "") or self.DEFAULT_PORTS)
        self.sample_per_24 = max(1, _int("SAMPLE_PER_24", 2))
        # floors: zero/negative timeouts would make every probe fail (or the
        # feed refresher busy-loop) silently on an unattended deployment
        self.connect_timeout = max(0.5, _float("CONNECT_TIMEOUT", 2.5))
        self.probe_timeout = max(1.0, _float("PROBE_TIMEOUT", 6.0))
        self.chat_timeout = max(1.0, _float("CHAT_TIMEOUT", 30.0))
        self.data_dir = os.environ.get("DATA_DIR", "") or "/data"
        self.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.llm_test_api_key = os.environ.get("LLM_TEST_API_KEY", "test").strip()
        # budget must survive reasoning models: they may burn tokens thinking
        # before emitting any answer, so a tiny cap can come back empty
        self.llm_max_tokens = max(16, _int("LLM_MAX_TOKENS", 256))
        self.page_title = os.environ.get("PAGE_TITLE", "") or "Coming soon"
        self.page_content = (os.environ.get("PAGE_CONTENT", "")
                             or "This site is under construction and will be back shortly.")
        self.feed_refresh_hours = max(0.1, _float("FEED_REFRESH_HOURS", 6.0))
        self.providers = [p.strip().lower() for p in
                          (os.environ.get("PROVIDERS", "") or self.DEFAULT_PROVIDERS).split(",")
                          if p.strip()]
        self.selftest = _bool("SELFTEST", False)

    @staticmethod
    def _ports(raw):
        out = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                p = int(part)
            except ValueError:
                continue
            if 1 <= p <= 65535:
                out.append(p)
        if not out:
            if raw.strip():
                log.warning("SCAN_PORTS=%r parsed to no valid ports — "
                            "falling back to port 80", raw)
            return [80]
        return out

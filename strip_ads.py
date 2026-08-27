import json
import re
from mitmproxy import http

# Exact fields confirmd via mitmproxy captures / community reverse-engineering
AD_INDICATOR_KEYS = {
    "is_promoted",
    "is_downstream_promotion",
    "pin_promotion_id",
    "ad_data",
    "advertiser_id",
    "sponsorship",
    "promoted_is_auto_assembled",
    "promoted_is_showcase",
    "promoted_is_lead_ad",
    "promoted_is_catalog_carousel_ad",
    "promoted_is_max_video",
    "promoted_quiz_pin_data",
    "ad_destination_url",
}


# Word roots seen across every known ad-indicatior field, snake_case or
# camelCase.
AD_TOKENS = {
    "ad", "ads",
    "promoted", "promotion", "promotions",
    "sponsor", "sponsored", "sponsorship",
    "advertiser", "advertisers", "advertising",
    "campaign", "campaigns",
    "adgroup",
}

PREFILTER_SUBSTRINGS = (
    "prompt", "sponsor", "advertis", "campaign", "adgroup",
)


_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")

def _tokens(key: str) -> list[str]:
    snaked = _CAMEL_BOUNDARY.sub(r"\1_\2", key)
    return [t.lower() for t in snaked.split("_") if t]

def is_ad_key(key: str) -> bool:
    return not AD_TOKENS.isdisjoint(_tokens(key))


def _truthy(val) -> bool:
    if val is True:
        return True
    if isinstance(val, str):
        return bool(val.strip())
    if isinstance(val, dict):
        return bool(val)
    if isinstance(val, (int, float)):
        return bool(val)
    return False


def is_ad_pin(obj: dict) -> bool:
    for key, val in obj.items():
        if key in AD_INDICATOR_KEYS or is_ad_key(key):
            if _truthy(val):
                return True
    return False


def scrub(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            scrub(v)
    elif isinstance(obj, list):
        i = 0
        while i < len(obj):
            item = obj[i]
            if isinstance(item, dict) and is_ad_pin(item):
                del obj[i]
                continue
            scrub(item)
            i += 1


def is_pinterest(flow: http.HTTPFlow) -> bool:
    host = (flow.request.pretty_host or "").lower()
    sni = (getattr(flow.client_conn, "sni", None) or "").lower()
    return "pinterest.com" in host or "pinterest.com" in sni or "pinimg.com" in host or "pinimg.com" in sni


def response(flow: http.HTTPFlow) -> None:
    if not is_pinterest(flow):
        return

    content_type = flow.response.headers.get("content-type", "")

    if "application/json" in content_type:
        try:
            text = flow.response.get_text()
            if not any(s in text for s in PREFILTER_SUBSTRINGS) and not any(
                key in text for key in AD_INDICATOR_KEYS
            ):
                return
            data = json.loads(text)
            scrub(data)
            flow.response.set_text(json.dumps(data))
        except (json.JSONDecodeError, ValueError):
            pass

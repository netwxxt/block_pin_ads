import json
from mitmproxy import http

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


def is_ad_pin(obj: dict) -> bool:
    for key in AD_INDICATOR_KEYS:
        if key not in obj:
            continue
        val = obj[key]
        if val is True:
            return True
        if isinstance(val, str) and val.strip():
            return True
        if isinstance(val, dict) and val:
            return True
        if isinstance(val, (int, float)) and val:
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
            if not any(key in text for key in AD_INDICATOR_KEYS):
                return
            data = json.loads(text)
            scrub(data)
            flow.response.set_text(json.dumps(data))
        except (json.JSONDecodeError, ValueError):
            pass

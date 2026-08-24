import json
from mitmproxy import http

def strip_promotion(obj):
    if isinstance(obj, dict):
        obj.pop("pin_promotion_id", None)
        for v in obj.values():
            strip_promotion(v)
    elif isinstance(obj, list):
        for item in obj:
            strip_promotion(item)

def response(flow: http.HTTPFlow):
    if "pinterest.com" not in flow.request.pretty_host:
        return
    ct = flow.response.headers.get("content-type", "")
    if "json" in ct or "html" in ct:
        try:
            if "json" in ct:
                data = json.loads(flow.response.text)
                strip_promotion(data)
                flow.response.text = json.dumps(data)
            elif "html" in ct and "initialReduxState" in flow.response.text:
                # crude but works: regex out pin_promotion_id key-value pairs from embedded JSON
                flow.response.text = flow.response.text.replace(
                    '"pin_promotion_id"', '"_stripped_promo_id"'
                )
        except Exception:
            pass

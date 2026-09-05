"""The real number, when OpenRouter will tell us.

Everything else here counts its own calls and says "estimated", because a normal inference
key cannot see its own usage: successful responses carry no rate-limit headers and
/api/v1/key reports credits, which stay at zero on the free tier while the day is spent.

But the number does exist. `/api/v1/analytics/query` serves a `request_count` metric, and
`/api/v1/activity` serves per-endpoint history. Both answer 403 to an inference key:

    "Only management keys can access analytics"

A MANAGEMENT KEY is a second, separate credential from openrouter.ai/settings/management-keys.
It cannot make completions at all -- it is administrative only -- but it CAN list, create and
DELETE your API keys. That is more power than this program has any business exercising, so:

  * it is entirely optional. Without one, the estimate works exactly as before.
  * it is only ever sent to the analytics endpoints, and only ever to READ. Nothing here
    calls a key-management route, and there is a test asserting no such path appears in this
    file. A credential that could delete your keys must never be one keystroke from doing it.
  * it is stored the same way as the inference key, 0600, and never returned to the browser.
"""
from __future__ import annotations

import datetime
import json
import urllib.error
import urllib.request

ANALYTICS = "https://openrouter.ai/api/v1/analytics/query"
TIMEOUT = 20.0


def _utc_day_bounds(day: datetime.date | None = None) -> tuple[str, str]:
    d = day or datetime.datetime.now(datetime.timezone.utc).date()
    start = datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc)
    return (start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            (start + datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))


def requests_today(management_key: str, day: datetime.date | None = None) -> tuple[int | None, str]:
    """(count, what happened). None means we could not find out -- never a guessed zero.

    A failure here must not be reported as "you have used nothing". The caller falls back to
    its own estimate and keeps saying estimated, which is the honest state.
    """
    if not management_key:
        return None, "no management key set"
    start, end = _utc_day_bounds(day)
    body = json.dumps({
        "metrics": ["request_count"],
        "time_range": {"start": start, "end": end},
        "granularity": "day",
    }).encode()
    req = urllib.request.Request(ANALYTICS, data=body, headers={
        "Authorization": f"Bearer {management_key.strip()}",
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read()[:200].decode(errors="replace")
        if e.code == 403:
            return None, "that key cannot read analytics - a MANAGEMENT key is needed"
        if e.code == 401:
            return None, "OpenRouter rejected the management key"
        return None, f"analytics answered {e.code}: {raw}"
    except Exception as e:
        return None, f"could not reach analytics ({type(e).__name__})"

    rows = ((payload.get("data") or {}).get("data")) or []
    total = 0
    found = False
    for row in rows:
        v = row.get("request_count")
        if v is None:
            continue
        found = True
        total += int(v)
    if not found:
        # The shape is documented but unverified against a live management key. Saying "0"
        # here would turn "we could not read it" into "you have used nothing", which is the
        # worst possible direction for a quota meter to be wrong in.
        return None, "analytics answered but carried no request_count"
    return total, "read from OpenRouter analytics"

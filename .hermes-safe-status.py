import http.cookiejar
import json
import subprocess
import urllib.request

CLI = "/Users/y/.railway/bin/railway"
WORKDIR = "/Users/y/sdf"
BASE = "https://sdf-production-4f52.up.railway.app"
EXPECTED = {-1002229799107, -1002197448156}

raw = subprocess.run(
    [CLI, "variable", "list", "--service", "sdf", "--json"],
    cwd=WORKDIR,
    capture_output=True,
    text=True,
    check=True,
    timeout=90,
).stdout
variables = json.loads(raw)
cookies = http.cookiejar.CookieJar()
client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
login = urllib.request.Request(
    BASE + "/api/login",
    data=json.dumps(
        {
            "username": variables.get("DASHBOARD_USER", "admin"),
            "password": variables["DASHBOARD_PASS"],
        }
    ).encode(),
    headers={"Content-Type": "application/json"},
)
json.load(client.open(login, timeout=20))
status = json.load(client.open(BASE + "/api/status", timeout=30))
accounts = []
for index, account in enumerate(status.get("accounts") or [], 1):
    persona = account.get("persona") or {}
    if isinstance(persona, str):
        try:
            persona = json.loads(persona)
        except (TypeError, ValueError):
            persona = {}
    accounts.append(
        {
            "account": index,
            "age": persona.get("age"),
            "gender": persona.get("gender"),
            "city": persona.get("city"),
            "running": bool(account.get("is_running")),
            "state": account.get("state"),
            "both_groups": {int(value) for value in (account.get("groups") or [])} == EXPECTED,
            "setup_complete": bool(account.get("setup_complete")),
            "errors": (account.get("stats") or {}).get("errors", 0),
        }
    )
out = {
    "total": status.get("total"),
    "running": status.get("running"),
    "proactive_loop_seconds": status.get("proactive_loop_seconds"),
    "water_cross_talk_probability": status.get("water_cross_talk_probability"),
    "features": status.get("features"),
    "accounts": accounts,
    "vars": {
        key: variables.get(key, "<default>")
        for key in (
            "PROACTIVE_MIN_INTERVAL_MINUTES",
            "PROACTIVE_MAX_PER_DAY",
            "PROACTIVE_LOOP_MIN_SECONDS",
            "PROACTIVE_LOOP_MAX_SECONDS",
            "MIN_TYPING_DELAY",
            "MAX_TYPING_DELAY",
            "BASE_REPLY_PROBABILITY",
            "WATER_CROSS_TALK_PROBABILITY",
            "CONTINUOUS_ACTIVITY_MODE",
            "CONTINUOUS_ACTIVITY_INTERVAL_SECONDS",
            "AI_MODEL",
        )
    },
}
print(json.dumps(out, ensure_ascii=False, indent=2))

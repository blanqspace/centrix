# my_tools/check_scopes.py
import os
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

NEED = {
    "chat:write","channels:read","groups:read","im:read","mpim:read",
    "commands","app_mentions:read","users:read"
}

def main():
    tok = os.environ.get("SLACK_BOT_TOKEN")
    if not tok:
        raise SystemExit("ERROR: SLACK_BOT_TOKEN fehlt (source .env)")

    c = WebClient(token=tok)

    # 1) Basistest
    who = c.auth_test()
    print("auth_test OK:", {k: who[k] for k in ("bot_id","user_id","team_id") if k in who})

    scopes = set()
    err_msgs = []

    # 2) Versuch A: auth.scopes
    try:
        r = c.api_call("auth.scopes")
        if r.get("ok") and "scopes" in r:
            bot_scopes = r["scopes"].get("bot", []) or r["scopes"].get("app_home", [])
            scopes = set(bot_scopes)
            print("auth.scopes:", sorted(scopes))
    except SlackApiError as e:
        err_msgs.append(f"auth.scopes -> {e.response.data.get('error')}")

    # 3) Versuch B: apps.permissions.scopes.list (Fallback)
    if not scopes:
        try:
            r = c.api_call("apps.permissions.scopes.list")
            if r.get("ok"):
                # 'scopes': {'app_home': [...], 'workspace': [...] } je nach App
                all_lists = []
                s = r.get("scopes", {})
                for k, v in s.items():
                    if isinstance(v, list):
                        all_lists.extend(v)
                scopes = set(all_lists)
                print("apps.permissions.scopes.list:", sorted(scopes))
        except SlackApiError as e:
            err_msgs.append(f"apps.permissions.scopes.list -> {e.response.data.get('error')}")

    if not scopes:
        print("WARN: Konnte Scopes nicht ermitteln.", "; ".join(err_msgs))
        return

    missing = sorted(NEED - scopes)
    print("MISSING:", missing if missing else "OK")

if __name__ == "__main__":
    main()

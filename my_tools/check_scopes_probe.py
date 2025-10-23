# my_tools/check_scopes_probe.py
import os
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

BOT = os.environ["SLACK_BOT_TOKEN"]
CHAN = os.environ["SLACK_CHANNEL_CONTROL"]  # Kanal-ID
c = WebClient(token=BOT)

tests = [
    # (beschreibung, callable, erwarteter_scope)
    ("chat.postMessage (chat:write)",
     lambda: c.chat_postMessage(channel=CHAN, text="scope-probe: chat.write OK?"),
     "chat:write"),
    ("conversations.list (channels:read)",
     lambda: c.conversations_list(types="im", limit=1),
     "channels:read"),
    ("users.info (users:read)",
     lambda: c.users_info(user=c.auth_test()["user_id"]),
     "users:read"),
    ("conversations.members (groups:read/channels:read)",
     lambda: c.conversations_members(channel=CHAN, limit=1),
     "channels:read oder groups:read"),
    ("im.list (im:read)",
     lambda: c.conversations_list(types="im", limit=1),
     "im:read"),
]

missing = []
for name, fn, scope in tests:
    try:
        fn()
        print(f"[OK] {name}")
    except SlackApiError as e:
        err = e.response.data.get("error")
        print(f"[FAIL] {name} -> {err}")
        if err in {"missing_scope", "not_allowed_token_type", "invalid_auth", "account_inactive"}:
            missing.append((scope, err))

if missing:
    print("\nFehlende/inkompatible Scopes erkannt:")
    for scope, err in missing:
        print(f"- {scope} ({err})")
else:
    print("\nAlle getesteten Scopes funktionsfähig.")

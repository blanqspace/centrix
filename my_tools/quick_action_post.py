# quick_action_post.py
import os
from slack_sdk import WebClient
c = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
c.chat_postMessage(
  channel=os.environ["SLACK_CHANNEL_CONTROL"],
  text="Approve?",
  blocks=[
    {"type":"section","text":{"type":"mrkdwn","text":"Selftest: approve?"}},
    {"type":"actions","elements":[
      {"type":"button","text":{"type":"plain_text","text":"Approve"},
       "style":"primary","action_id":"approve_btn"}
    ]}
  ]
)
print("posted")

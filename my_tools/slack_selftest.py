from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError
import os, time

app = App(token=os.environ["SLACK_BOT_TOKEN"])

@app.event("app_mention")
def handle_mention(body, say):
    user = body["event"]["user"]
    say(f"<@{user}> mention received")

@app.action("approve_btn")
def handle_approve(ack, body, say, logger):
    ack()
    user = body["user"]["id"]
    say(f"✅ approved by <@{user}>")
    logger.info("action approve_btn received")

def main():
    print("=== Slack Selftest ===")
    print("Verbindung prüfen ...")
    who = app.client.auth_test()
    print("Bot-ID:", who.get("bot_id"), "User-ID:", who.get("user_id"))
    try:
        channel = os.environ["SLACK_CHANNEL_CONTROL"]
        app.client.chat_postMessage(
            channel=channel,
            text="Selftest: approve?",
            blocks=[
                {"type": "section",
                 "text": {"type": "mrkdwn", "text": "Selftest: approve?"}},
                {"type": "actions",
                 "elements": [
                     {"type": "button",
                      "text": {"type": "plain_text", "text": "Approve"},
                      "style": "primary",
                      "action_id": "approve_btn"}]}
            ],
        )
        print("Testnachricht gesendet. Bitte Button in Slack klicken.")
    except SlackApiError as e:
        print("Fehler beim Senden:", e.response["error"])

    print("Socket-Mode startet … (60 Sekunden aktiv)")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.connect()
    time.sleep(60)
    handler.close()
    print("Selftest beendet.")

if __name__ == "__main__":
    main()

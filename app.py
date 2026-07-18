"""
Lead Nurture Bot — WhatsApp Edition
--------------------------------------
The real problem: brokers don't lack judgment, they lack PERSISTENCE.
This bot remembers every lead forwarded to it, and reminds the broker
to follow up at the right intervals — 3 days, 1 week, 3 weeks, monthly —
so no lead is ever silently forgotten, even 6 months later.
"""

import os
import json
import time
from datetime import datetime, timedelta
from flask import Flask, request
from groq import Groq
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
twilio_client = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")  # e.g. "whatsapp:+14155238886"

MODEL = "llama-3.3-70b-versatile"
LEADS_FILE = "leads.json"

# Follow-up schedule: how many days after first contact to nudge the broker
FOLLOWUP_SCHEDULE = [3, 7, 21, 30]  # then repeats monthly after day 30


# ---------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------
def load_leads():
    return json.load(open(LEADS_FILE)) if os.path.exists(LEADS_FILE) else {}

def save_leads(leads):
    json.dump(leads, open(LEADS_FILE, "w"), indent=2)


# ---------------------------------------------------------------
# STEP 1: Receiving a WhatsApp message — could be a new lead,
# or a command like "status" or "done 2"
# ---------------------------------------------------------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    incoming_msg = request.form.get("Body", "").strip()
    from_number = request.form.get("From", "")

    resp = MessagingResponse()
    command = incoming_msg.lower()

    try:
        if command in ("status", "list"):
            resp.message(get_status_message(from_number))

        elif command.startswith("done") or command.startswith("close"):
            reply_text = mark_lead_done(from_number, command)
            resp.message(reply_text)

        else:
            # Not a command - treat as a new lead
            reply_text = log_new_lead(from_number, incoming_msg)
            resp.message(reply_text)

    except Exception as e:
        print(f"  ⚠️ Error handling incoming message: {e}")
        resp.message("Something went wrong on my end - your message wasn't lost, please try again in a moment.")

    return str(resp)


SYSTEM_PROMPT = """You are a sharp real estate lead qualifier in Bangalore. Score
the lead as HOT, WARM, or COLD based on real buying signals:
- HOT: specific property/area mentioned, clear timeline, financing readiness
  (loan pre-approved, cash buyer), or explicit next-step request (site visit, call)
- WARM: general interest in an area/property type, but vague timeline, still researching
- COLD: mass-inquiry language ("please send brochure"), no specific property mentioned

Respond with ONLY one word: HOT, WARM, or COLD. Nothing else.
"""

def score_lead(message_text, max_retries=2):
    """Quick single-word scoring - used to prioritize the status list."""
    for attempt in range(max_retries + 1):
        try:
            response = groq_client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": message_text}
                ],
                temperature=0.3,
                timeout=10
            )
            score = response.choices[0].message.content.strip().upper()
            if score in ("HOT", "WARM", "COLD"):
                return score
            return "WARM"  # safe default if model returns something unexpected
        except Exception as e:
            print(f"  ⚠️ Scoring attempt {attempt + 1} failed: {e}")
            if attempt < max_retries:
                time.sleep(2)
    return "WARM"  # safe fallback if all retries fail


def log_new_lead(from_number, incoming_msg):
    leads = load_leads()
    lead_id = f"{from_number}_{int(time.time())}"
    score = score_lead(incoming_msg)

    leads[lead_id] = {
        "broker_number": from_number,
        "message": incoming_msg,
        "date_received": datetime.now().isoformat(),
        "followups_sent": [],
        "status": "active",
        "score": score
    }
    save_leads(leads)

    score_emoji = {"HOT": "🔥", "WARM": "🌤️", "COLD": "❄️"}
    return (
        f"Got it! {score_emoji.get(score, '')} Scored as {score}.\n"
        "I'll remind you when it's time to follow up — 3 days, 1 week, 3 weeks, "
        "monthly after that. Nothing will slip through. 👍\n\n"
        "Reply \"status\" anytime to see all your active leads."
    )


def get_active_leads_for_broker(from_number):
    """Returns this broker's active (not-yet-closed) leads, HOT first,
    then WARM, then COLD - so the most important ones show up on top."""
    leads = load_leads()
    active = [
        (lead_id, lead) for lead_id, lead in leads.items()
        if lead["broker_number"] == from_number and lead.get("status", "active") == "active"
    ]
    score_order = {"HOT": 0, "WARM": 1, "COLD": 2}
    active.sort(key=lambda item: (
        score_order.get(item[1].get("score", "WARM"), 1),
        item[1]["date_received"]
    ))
    return active


def get_status_message(from_number):
    active = get_active_leads_for_broker(from_number)

    if not active:
        return "You have no active leads being tracked right now. Forward me a lead to get started!"

    score_emoji = {"HOT": "🔥", "WARM": "🌤️", "COLD": "❄️"}
    lines = ["📋 Your active leads (priority order):\n"]
    for i, (lead_id, lead) in enumerate(active, 1):
        days_since = (datetime.now() - datetime.fromisoformat(lead["date_received"])).days
        snippet = lead["message"][:45] + ("..." if len(lead["message"]) > 45 else "")
        emoji = score_emoji.get(lead.get("score", "WARM"), "")
        lines.append(f"{i}. {emoji} ({days_since}d ago) {snippet}")

    lines.append("\nReply \"done 2\" to stop tracking lead #2 (or whichever number applies).")
    return "\n".join(lines)


def mark_lead_done(from_number, command):
    parts = command.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return "To close a lead, reply like this: \"done 2\" (using the number from \"status\")."

    index = int(parts[1])
    active = get_active_leads_for_broker(from_number)

    if index < 1 or index > len(active):
        return f"I don't see lead #{index}. Reply \"status\" to see your current active leads."

    lead_id, lead = active[index - 1]
    leads = load_leads()
    leads[lead_id]["status"] = "closed"
    save_leads(leads)

    return f"✅ Marked lead #{index} as done. No more follow-ups for that one."


# ---------------------------------------------------------------
# STEP 2: The daily check — who needs a nudge today?
# This gets triggered by a free external cron service (cron-job.org),
# since Render's free tier can't run background schedules on its own.
# ---------------------------------------------------------------
@app.route("/check-followups", methods=["GET", "POST"])
def check_followups():
    leads = load_leads()
    nudges_sent = 0
    errors = []

    for lead_id, lead in leads.items():
        try:
            if lead.get("status", "active") == "closed":
                continue

            date_received = datetime.fromisoformat(lead["date_received"])
            days_since = (datetime.now() - date_received).days

            due_day = None
            for day in FOLLOWUP_SCHEDULE:
                if days_since >= day and day not in lead["followups_sent"]:
                    due_day = day

            if due_day is None and days_since > 30:
                months_passed = days_since // 30
                monthly_marker = f"month_{months_passed}"
                if monthly_marker not in lead["followups_sent"]:
                    due_day = monthly_marker

            if due_day is not None:
                nudge_text = draft_followup_nudge(lead["message"], days_since)
                send_whatsapp_message(lead["broker_number"], nudge_text)
                lead["followups_sent"].append(due_day)
                nudges_sent += 1

        except Exception as e:
            # One lead failing should never stop the rest from being processed
            print(f"  ⚠️ Failed to process lead {lead_id}: {e}")
            errors.append(str(lead_id))
            continue

    save_leads(leads)
    return {"status": "ok", "nudges_sent": nudges_sent, "errors": errors}


def draft_followup_nudge(original_message, days_since, max_retries=2):
    """Uses AI to draft a short, useful check-in the broker can send —
    not a generic 'still interested?' but something that references the
    original inquiry, so it feels personal, not automated.
    Retries on failure, falls back to a safe generic nudge if AI is unavailable."""
    prompt = f"""A real estate broker in Bangalore received this lead {days_since} days ago:
"{original_message}"

Write a short, natural WhatsApp follow-up message (2-3 sentences max) the broker
can send to check in with this buyer. Reference something specific from their
original message. Keep it warm and low-pressure, not salesy. Do not use placeholder
brackets - write it as ready-to-send text."""

    draft = None
    for attempt in range(max_retries + 1):
        try:
            response = groq_client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                timeout=15
            )
            draft = response.choices[0].message.content.strip()
            break
        except Exception as e:
            print(f"  ⚠️ Nudge drafting attempt {attempt + 1} failed: {e}")
            if attempt < max_retries:
                time.sleep(2)

    if draft is None:
        # Safe fallback - still nudges the broker even if AI is down
        draft = "Just checking in on this lead - worth a quick follow-up message?"

    return (f"⏰ Reminder: it's been {days_since} days since this lead:\n"
            f"\"{original_message[:100]}\"\n\n"
            f"Here's a ready-to-send follow-up:\n\n{draft}")


def send_whatsapp_message(to_number, body):
    twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_NUMBER,
        to=to_number,
        body=body
    )


@app.route("/debug/leads")
def debug_leads():
    """Quick way to see what's actually stored right now, for testing."""
    return load_leads()


@app.route("/")
def home():
    return "Lead Nurture Bot is running. Forward a lead via WhatsApp to test."


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

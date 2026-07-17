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
# STEP 1: Receiving a forwarded lead on WhatsApp
# ---------------------------------------------------------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    incoming_msg = request.form.get("Body", "")
    from_number = request.form.get("From", "")  # the broker's WhatsApp number

    leads = load_leads()
    lead_id = f"{from_number}_{int(time.time())}"

    leads[lead_id] = {
        "broker_number": from_number,
        "message": incoming_msg,
        "date_received": datetime.now().isoformat(),
        "followups_sent": []  # tracks which schedule days we've already nudged for
    }
    save_leads(leads)

    resp = MessagingResponse()
    resp.message(
        "Got it! I've logged this lead and will remind you when it's time to follow up — "
        "3 days, 1 week, 3 weeks, and monthly after that. Nothing will slip through. 👍"
    )
    return str(resp)


# ---------------------------------------------------------------
# STEP 2: The daily check — who needs a nudge today?
# This gets triggered by a free external cron service (cron-job.org),
# since Render's free tier can't run background schedules on its own.
# ---------------------------------------------------------------
@app.route("/check-followups", methods=["GET", "POST"])
def check_followups():
    leads = load_leads()
    nudges_sent = 0

    for lead_id, lead in leads.items():
        date_received = datetime.fromisoformat(lead["date_received"])
        days_since = (datetime.now() - date_received).days

        # Figure out which schedule day (if any) matches today
        due_day = None
        for day in FOLLOWUP_SCHEDULE:
            if days_since >= day and day not in lead["followups_sent"]:
                due_day = day

        # After day 30, repeat monthly (every 30 days) if not already nudged this month
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

    save_leads(leads)
    return {"status": "ok", "nudges_sent": nudges_sent}


def draft_followup_nudge(original_message, days_since):
    """Uses AI to draft a short, useful check-in the broker can send —
    not a generic 'still interested?' but something that references the
    original inquiry, so it feels personal, not automated."""
    prompt = f"""A real estate broker in Bangalore received this lead {days_since} days ago:
"{original_message}"

Write a short, natural WhatsApp follow-up message (2-3 sentences max) the broker
can send to check in with this buyer. Reference something specific from their
original message. Keep it warm and low-pressure, not salesy. Do not use placeholder
brackets - write it as ready-to-send text."""

    response = groq_client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.6
    )
    draft = response.choices[0].message.content.strip()

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

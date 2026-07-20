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
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
twilio_client = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

MODEL = "llama-3.3-70b-versatile"

FOLLOWUP_SCHEDULE = [3, 7, 21, 30]


# ---------------------------------------------------------------
# Storage helpers — now backed by a real database (Supabase/Postgres),
# so data survives restarts. Each function talks to just the rows it needs,
# instead of loading/saving one giant file every time.
# ---------------------------------------------------------------
def insert_lead(lead_id, broker_number, message, score):
    supabase.table("leads").insert({
        "lead_id": lead_id,
        "broker_number": broker_number,
        "message": message,
        "date_received": datetime.now().isoformat(),
        "followups_sent": json.dumps([]),
        "status": "active",
        "score": score
    }).execute()


def get_active_leads_for_broker_db(broker_number):
    result = supabase.table("leads").select("*").eq("broker_number", broker_number).eq("status", "active").execute()
    return result.data


def get_all_active_leads_db():
    result = supabase.table("leads").select("*").eq("status", "active").execute()
    return result.data


def update_lead_status(lead_id, status):
    supabase.table("leads").update({"status": status}).eq("lead_id", lead_id).execute()


def update_lead_followups(lead_id, followups_sent):
    supabase.table("leads").update({"followups_sent": json.dumps(followups_sent)}).eq("lead_id", lead_id).execute()


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

        elif command in ("help", "hi", "hello", "start"):
            resp.message(
                "👋 Here's what I can do:\n\n"
                "• Forward me any lead message and I'll log + score it\n"
                "• Reply \"status\" to see all your active leads\n"
                "• Reply \"done 2\" to stop tracking lead #2\n\n"
                "Try forwarding a real lead now to see it in action!"
            )

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
- WARM: general interest in an area/property type, but vague timeline, still researching,
  OR the person explicitly states interest and asks for more details (engagement signal)
- COLD: mass-inquiry language ("please send brochure"), no specific property mentioned,
  OR a bare price-only question with zero other context (classic multi-agent comparison
  shopping behavior, not real engagement)

Respond with ONLY one word: HOT, WARM, or COLD. Nothing else.

Two important distinctions, based on real cases that are easy to get wrong:

1. "might be interested eventually, send more details please" -> WARM, not COLD.
   Even though there's no timeline, the person explicitly said "interested" and
   actively asked for more details - that's real engagement, just early-stage.

2. "what's the price of this property" (with NO other context - no stated interest,
   no property specifics, no follow-up ask) -> COLD, not WARM.
   A bare price question alone is classic behavior for someone comparing many
   agents/portals at once, not a real engaged buyer.

The difference: stated interest + an ask for more info = WARM. A bare transactional
question with zero engagement language = COLD.
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
    # Edge case: broker forwards a photo/voice note with no text caption
    if not incoming_msg.strip():
        return (
            "I got your message, but there's no text to log — if you forwarded a "
            "photo or voice note, please add a short caption describing the lead "
            "(e.g. \"3BHK HSR, budget 1.2Cr\") so I can track it properly."
        )

    lead_id = f"{from_number}_{int(time.time())}"
    score = score_lead(incoming_msg)
    insert_lead(lead_id, from_number, incoming_msg, score)

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
    active = get_active_leads_for_broker_db(from_number)
    score_order = {"HOT": 0, "WARM": 1, "COLD": 2}
    active.sort(key=lambda lead: (
        score_order.get(lead.get("score", "WARM"), 1),
        lead["date_received"]
    ))
    return active


def get_status_message(from_number):
    active = get_active_leads_for_broker(from_number)

    if not active:
        return "You have no active leads being tracked right now. Forward me a lead to get started!"

    score_emoji = {"HOT": "🔥", "WARM": "🌤️", "COLD": "❄️"}
    lines = ["📋 Your active leads (priority order):\n"]
    for i, lead in enumerate(active, 1):
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

    lead = active[index - 1]
    update_lead_status(lead["lead_id"], "closed")

    return f"✅ Marked lead #{index} as done. No more follow-ups for that one."


# ---------------------------------------------------------------
# STEP 2: The daily check — who needs a nudge today?
# This gets triggered by a free external cron service (cron-job.org),
# since Render's free tier can't run background schedules on its own.
# ---------------------------------------------------------------
@app.route("/check-followups", methods=["GET", "POST"])
def check_followups():
    active_leads = get_all_active_leads_db()
    nudges_sent = 0
    errors = []

    for lead in active_leads:
        try:
            lead_id = lead["lead_id"]
            followups_sent = json.loads(lead.get("followups_sent") or "[]")
            date_received = datetime.fromisoformat(lead["date_received"])
            days_since = (datetime.now() - date_received).days

            due_day = None
            for day in FOLLOWUP_SCHEDULE:
                if days_since >= day and day not in followups_sent:
                    due_day = day

            if due_day is None and days_since > 30:
                months_passed = days_since // 30
                monthly_marker = f"month_{months_passed}"
                if monthly_marker not in followups_sent:
                    due_day = monthly_marker

            if due_day is not None:
                nudge_text = draft_followup_nudge(lead["message"], days_since)
                send_whatsapp_message(lead["broker_number"], nudge_text)
                followups_sent.append(due_day)
                update_lead_followups(lead_id, followups_sent)
                nudges_sent += 1

        except Exception as e:
            print(f"  ⚠️ Failed to process lead {lead.get('lead_id')}: {e}")
            errors.append(str(lead.get("lead_id")))
            continue

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
    result = supabase.table("leads").select("*").execute()
    return {"leads": result.data}


@app.route("/")
def home():
    return "Lead Nurture Bot is running. Forward a lead via WhatsApp to test."


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

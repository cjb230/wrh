"""Sorting email into personal, list, automated, bulk and spam.

Rules, first match wins:

1. **spam**: Gmail put it in Spam, or a spam filter flagged it (X-Spam-Flag: YES).
2. **personal**: you sent it, or it is from someone you have written to.
   Your own correspondence always wins over the rules below.
3. **bulk**: newsletters and marketing. Gmail's Promotions tab, or headers
   only mailing tools add: List-Unsubscribe together with an email service
   provider's id (Mailchimp, SendGrid, Mailgun, Amazon SES campaigns, ...),
   Precedence: bulk/junk, or List-Unsubscribe without a List-Id.
4. **automated**: notifications and receipts. Auto-Submitted, a no-reply
   style sender, or Gmail's Updates or Social tab.
5. **list**: discussion mailing lists (List-Id, or Gmail's Forums tab).
6. **personal**: everything else, including strangers writing to you.

The result is a (category, reason) pair; the reason is stored alongside, so
a misfiled message can be traced to the rule that caught it.
"""

from __future__ import annotations

import re

CATEGORIES = ("personal", "list", "automated", "bulk", "spam")

# What to keep for each category, unless config.toml's [email.keep] says otherwise:
#   full     the whole message, with attachments stored separately
#   headers  sender, recipients, subject and date only
#   none     nothing (the message is skipped)
DEFAULT_KEEP = {"personal": "full", "list": "full", "automated": "full", "bulk": "none", "spam": "none"}

# Categories left off the timeline (they stay in email_messages).
TIMELINE_EXCLUDE = {"bulk", "spam"}

# Headers set by bulk email services. Their presence alone is not enough
# (receipts use them too), but with List-Unsubscribe it means a campaign.
ESP_HEADERS = (
    "x-mailchimp-campaign", "x-mc-user", "x-campaign", "x-campaign-id", "x-campaignid",
    "x-sg-eid", "x-sendgrid-eid", "x-mailgun-tag", "x-mailgun-sid", "x-ses-outgoing",
    "x-mailjet-campaign", "x-sib-id", "x-mkto", "x-marketo", "x-hubspot", "x-hs-",
    "x-ac-tid", "x-klaviyo", "x-cmail", "x-newsletter", "x-mailer-recptid", "x-rpcampaign",
    "x-emarsys", "x-ems-", "x-ibm-campaign", "x-substack", "x-convertkit", "x-beehiiv",
)
ESP_MAILERS = re.compile(r"(?i)mailchimp|sendgrid|mailgun|mailjet|sendinblue|brevo|hubspot|marketo|"
                         r"klaviyo|campaign monitor|constant contact|getresponse|acoustic|salesforce marketing|"
                         r"exacttarget|substack|convertkit|beehiiv|freshmail|smsapi|emarsys")
NOREPLY = re.compile(r"(?i)^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|notifications?|notify|alerts?|"
                     r"mailer-daemon|postmaster|bounces?|automated|auto-confirm|info|news(letter)?|"
                     r"marketing|offers|promo(tions)?|hello|team|updates?|support|billing|receipts?)([+@]|$)")

GMAIL_CATEGORY = {
    # Our own markers from IMAP X-GM-RAW searches, and Takeout's X-Gmail-Labels.
    "CATEGORY_PROMOTIONS": "bulk", "Category Promotions": "bulk", "Promotions": "bulk",
    "CATEGORY_UPDATES": "automated", "Category Updates": "automated",
    "CATEGORY_SOCIAL": "automated", "Category Social": "automated",
    "CATEGORY_FORUMS": "list", "Category Forums": "list",
}


def _h(headers, name: str) -> str:
    v = headers.get(name)
    return str(v).strip() if v is not None else ""


def _has_esp_header(headers) -> str | None:
    keys = [k.lower() for k in headers.keys()]
    for e in ESP_HEADERS:
        for k in keys:
            if k == e or (e.endswith("-") and k.startswith(e)):
                return k
    mailer = _h(headers, "X-Mailer")
    if mailer and ESP_MAILERS.search(mailer):
        return "x-mailer: " + mailer[:40]
    return None


def classify(headers, *, from_address: str | None, labels=(), is_sent: bool = False,
             correspondents: set[str] = frozenset()) -> tuple[str, str]:
    """``headers``: an email.message.Message (only its headers are read)."""
    labels = set(labels or ())
    if labels & {"\\Spam", "Spam"}:
        return "spam", "gmail spam"
    if _h(headers, "X-Spam-Flag").upper().startswith("YES"):
        return "spam", "x-spam-flag"

    if is_sent:
        return "personal", "sent by you"
    if from_address and from_address.lower() in correspondents:
        return "personal", "you have written to this sender"

    gmail = {GMAIL_CATEGORY[x] for x in labels if x in GMAIL_CATEGORY}
    unsub = _h(headers, "List-Unsubscribe")
    list_id = _h(headers, "List-Id")
    precedence = _h(headers, "Precedence").lower()

    if "bulk" in gmail:
        return "bulk", "gmail promotions"
    if precedence in ("bulk", "junk"):
        return "bulk", f"precedence: {precedence}"
    esp = _has_esp_header(headers)
    if unsub and esp:
        return "bulk", f"list-unsubscribe + {esp}"
    if unsub and not list_id:
        return "bulk", "list-unsubscribe"

    auto = _h(headers, "Auto-Submitted").lower()
    if auto and auto != "no":
        return "automated", f"auto-submitted: {auto}"
    local = (from_address or "").split("@", 1)[0]
    if from_address and NOREPLY.match(local + "@"):
        return "automated", f"sender {local}@"
    if "automated" in gmail:
        return "automated", "gmail updates/social"

    if list_id or precedence == "list" or "list" in gmail:
        return "list", "mailing list"
    return "personal", "default"

"""
Branded transactional email — one shell, several messages.

Every email the platform sends is built from `render()` below, so the header,
the button, the footer and the colour of the accent rule are defined exactly
once. Adding a message means writing the copy, not another 200 lines of
tables.

Why it looks like 2003 HTML
---------------------------
Because email clients are 2003 renderers. Outlook composes with the Word
engine, Gmail strips `<head>` styles on some clients and keeps them on others,
and none of them support flexbox, grid or external stylesheets. The rules this
file follows are the ones that survive everywhere:

* Layout is nested tables with explicit widths. No divs for structure.
* Every style that matters is inline on the element it styles.
* One 600px column — the widest that does not scroll horizontally in an
  Outlook reading pane.
* `<head>` CSS carries the mobile media query *only*. If it is stripped, the
  email still renders correctly, just at desktop widths.
* The logo is a remote image, and most clients block remote images by
  default on first view. So the logo carries `alt` text styled to look like
  the wordmark, which means a blocked-image render still shows "COACH AUTO"
  in brand red rather than a broken-image icon.
* The call to action is a table cell with a background colour, not a styled
  `<a>` — Outlook ignores padding on inline elements, which turns a button
  into a bare underlined link.
* The destination URL is printed in full beneath the button. Corporate mail
  gateways rewrite or strip buttons; a copyable link is the fallback that
  always works, and on a password reset it is the difference between a
  locked-out client and a signed-in one.

Dark shell
----------
The site is near-black with a red accent and the email matches it, so the
message looks like it came from the same company as the page it links to.
`color-scheme` and `supported-color-schemes` are declared so clients that
auto-invert (Gmail on Android, Outlook dark mode) leave the palette alone
instead of inverting it into something muddy.
"""

from html import escape

from app.core.config import settings

# Pulled from the front end's design tokens (`frontend/src/index.css`) so the
# email and the website cannot drift apart.
BRAND = "#e5202c"
BRAND_DARK = "#c4141f"
INK_950 = "#08080a"
INK_900 = "#0b0b0d"
INK_850 = "#101013"
INK_600 = "#26262b"
CHALK_50 = "#ffffff"
CHALK_200 = "#d6d6dc"
CHALK_400 = "#9a9aa4"
CHALK_500 = "#74747e"

FONT_STACK = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"
)


def _button(label: str, url: str) -> str:
    """A call-to-action that survives Outlook.

    The colour lives on the `<td>` and the padding lives on the `<a>`, which is
    the combination every major client renders the same way.
    """
    return f"""
      <table role="presentation" border="0" cellpadding="0" cellspacing="0" style="margin:0 auto;">
        <tr>
          <td align="center" bgcolor="{BRAND}" style="border-radius:6px; background-color:{BRAND};">
            <a href="{escape(url, quote=True)}"
               target="_blank"
               style="display:inline-block; padding:16px 36px; font-family:{FONT_STACK};
                      font-size:15px; font-weight:700; letter-spacing:0.08em;
                      text-transform:uppercase; color:{CHALK_50}; text-decoration:none;
                      border-radius:6px; border:1px solid {BRAND_DARK};">{escape(label)}</a>
          </td>
        </tr>
      </table>
    """


def render(
    *,
    preheader: str,
    eyebrow: str,
    heading: str,
    paragraphs: list[str],
    cta_label: str | None = None,
    cta_url: str | None = None,
    footnote: str | None = None,
) -> str:
    """Compose one complete HTML email.

    `preheader` is the grey line of text a mail client shows next to the
    subject in the inbox list. Left unset, clients scrape it from the first
    visible text in the body, which on a branded email is the alt text of the
    logo — so every message in the coach's inbox would preview as
    "Coach Auto Coach Auto Coach Auto". It is set deliberately and then hidden.
    """
    body_blocks = "".join(
        f"""
        <p style="margin:0 0 18px; font-family:{FONT_STACK}; font-size:16px;
                  line-height:1.65; color:{CHALK_200};">{paragraph}</p>
        """
        for paragraph in paragraphs
    )

    cta_block = ""
    if cta_label and cta_url:
        cta_block = f"""
        <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0">
          <tr><td style="padding:14px 0 22px;" align="center">{_button(cta_label, cta_url)}</td></tr>
          <tr>
            <td align="center" style="padding:0 0 6px;">
              <p style="margin:0; font-family:{FONT_STACK}; font-size:12px; line-height:1.6;
                        color:{CHALK_500};">
                Button not working? Copy this link into your browser:
              </p>
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:0 0 8px;">
              <a href="{escape(cta_url, quote=True)}" target="_blank"
                 style="font-family:{FONT_STACK}; font-size:12px; line-height:1.6;
                        color:{CHALK_400}; text-decoration:underline; word-break:break-all;"
                 >{escape(cta_url)}</a>
            </td>
          </tr>
        </table>
        """

    footnote_block = ""
    if footnote:
        footnote_block = f"""
        <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0">
          <tr>
            <td style="padding:20px 0 0; border-top:1px solid {INK_600};">
              <p style="margin:0; font-family:{FONT_STACK}; font-size:13px; line-height:1.6;
                        color:{CHALK_500};">{footnote}</p>
            </td>
          </tr>
        </table>
        """

    logo_url = escape(settings.email_logo_url, quote=True)
    support = escape(settings.SUPPORT_EMAIL)
    site = escape(settings.FRONTEND_URL.rstrip("/"), quote=True)

    return f"""<!doctype html>
<html lang="en" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="x-apple-disable-message-reformatting">
  <meta name="color-scheme" content="dark">
  <meta name="supported-color-schemes" content="dark">
  <title>{escape(heading)}</title>
  <!--[if mso]>
  <noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript>
  <![endif]-->
  <style>
    /* Mobile only. If a client strips this block the desktop layout still
       renders correctly — nothing structural depends on it. */
    @media only screen and (max-width:620px) {{
      .shell {{ width:100% !important; }}
      .pad {{ padding-left:24px !important; padding-right:24px !important; }}
      .h1 {{ font-size:26px !important; line-height:1.2 !important; }}
    }}
    a {{ color:{BRAND}; }}
  </style>
</head>
<body style="margin:0; padding:0; width:100%; background-color:{INK_950};">

  <!-- Inbox preview line. Present in the DOM, invisible in the render. -->
  <div style="display:none; max-height:0; overflow:hidden; opacity:0; mso-hide:all;">
    {escape(preheader)}
  </div>

  <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0"
         style="background-color:{INK_950};">
    <tr>
      <td align="center" style="padding:32px 12px;">

        <table role="presentation" class="shell" width="600" border="0" cellpadding="0" cellspacing="0"
               style="width:600px; max-width:600px; background-color:{INK_900};
                      border:1px solid {INK_600}; border-radius:12px; overflow:hidden;">

          <!-- Brand bar -->
          <tr>
            <td align="center" bgcolor="{INK_850}"
                style="background-color:{INK_850}; padding:28px 32px 24px;
                       border-bottom:3px solid {BRAND};">
              <a href="{site}" target="_blank" style="text-decoration:none;">
                <img src="{logo_url}" width="188" alt="COACH AUTO"
                     style="display:block; width:188px; max-width:188px; height:auto; border:0;
                            font-family:{FONT_STACK}; font-size:20px; font-weight:800;
                            letter-spacing:0.14em; color:{BRAND}; text-decoration:none;">
              </a>
            </td>
          </tr>

          <!-- Message -->
          <tr>
            <td class="pad" style="padding:36px 44px 30px;">
              <p style="margin:0 0 10px; font-family:{FONT_STACK}; font-size:11px; font-weight:700;
                        letter-spacing:0.22em; text-transform:uppercase; color:{BRAND};">
                {escape(eyebrow)}
              </p>
              <h1 class="h1" style="margin:0 0 22px; font-family:{FONT_STACK}; font-size:30px;
                         line-height:1.15; font-weight:800; color:{CHALK_50};">
                {escape(heading)}
              </h1>
              {body_blocks}
              {cta_block}
              {footnote_block}
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td class="pad" align="center" bgcolor="{INK_850}"
                style="background-color:{INK_850}; padding:24px 44px 28px;
                       border-top:1px solid {INK_600};">
              <p style="margin:0 0 6px; font-family:{FONT_STACK}; font-size:13px;
                        font-weight:700; color:{CHALK_200}; letter-spacing:0.04em;">
                {escape(settings.BRAND_NAME)}
              </p>
              <p style="margin:0 0 12px; font-family:{FONT_STACK}; font-size:12px;
                        line-height:1.6; color:{CHALK_500};">
                {escape(settings.BUSINESS_NAME)} · Online strength &amp; nutrition coaching
              </p>
              <p style="margin:0; font-family:{FONT_STACK}; font-size:12px; line-height:1.6;
                        color:{CHALK_500};">
                Questions? Reply to this email or write to
                <a href="mailto:{support}" style="color:{CHALK_400}; text-decoration:underline;"
                   >{support}</a>
              </p>
            </td>
          </tr>
        </table>

        <p style="margin:18px 0 0; font-family:{FONT_STACK}; font-size:11px; line-height:1.6;
                  color:{CHALK_500}; max-width:600px;">
          This is an automated message about your {escape(settings.BRAND_NAME)} account.
        </p>

      </td>
    </tr>
  </table>
</body>
</html>"""


# --- The messages -------------------------------------------------------------
#
# Each returns (subject, plain_text, html). The plain-text half is not a
# throwaway: it is what a text-only client shows, what some spam filters score,
# and what appears if the HTML fails to parse. It says the same things in the
# same order, including the link.


def welcome(name: str, login_url: str) -> tuple[str, str, str]:
    subject = f"Welcome to {settings.BRAND_NAME} — your account is ready"

    text = (
        f"Hi {name},\n\n"
        f"Your {settings.BRAND_NAME} account is ready.\n\n"
        "Sign in and complete your intake — height, weight, tape measurements and your "
        "starting photos — so your coach can build your first block of training.\n\n"
        f"Sign in: {login_url}\n\n"
        "The starting photos matter more than they feel like they should. Twelve weeks "
        "from now they are the only honest record of where you began.\n\n"
        f"Questions? Reply to this email or write to {settings.SUPPORT_EMAIL}.\n\n"
        f"— {settings.BRAND_NAME} | {settings.BUSINESS_NAME}"
    )

    html = render(
        preheader="Your account is ready — sign in and complete your intake.",
        eyebrow="Account ready",
        heading=f"Welcome, {escape(name)}.",
        paragraphs=[
            f"Your {escape(settings.BRAND_NAME)} account is open. One step left before your "
            "coach can write your first block of training.",
            "Sign in and complete your intake: height, weight, tape measurements and your "
            "starting photos. It takes about five minutes.",
        ],
        cta_label="Complete your intake",
        cta_url=login_url,
        footnote=(
            "The starting photos matter more than they feel like they should. Twelve weeks "
            "from now they are the only honest record of where you began."
        ),
    )
    return subject, text, html


def password_reset(reset_url: str, expires_minutes: int) -> tuple[str, str, str]:
    subject = f"Reset your {settings.BRAND_NAME} password"

    text = (
        f"You asked to reset your {settings.BRAND_NAME} password.\n\n"
        f"Open this link to set a new one:\n{reset_url}\n\n"
        f"The link works for {expires_minutes} minutes and can only be used once.\n\n"
        "If you did not ask for this, you can ignore this email — nothing has changed "
        "and your current password still works.\n\n"
        f"— {settings.BRAND_NAME} | {settings.BUSINESS_NAME}"
    )

    html = render(
        preheader=f"Set a new password. This link expires in {expires_minutes} minutes.",
        eyebrow="Password reset",
        heading="Set a new password",
        paragraphs=[
            f"You asked to reset the password on your {escape(settings.BRAND_NAME)} account. "
            "Use the button below to choose a new one.",
        ],
        cta_label="Choose a new password",
        cta_url=reset_url,
        footnote=(
            f"This link expires in {expires_minutes} minutes and works once. "
            "If you did not request it you can safely ignore this email — nothing has "
            "changed, and your current password still works."
        ),
    )
    return subject, text, html


def password_changed(name: str, reset_url: str) -> tuple[str, str, str]:
    """Security notice after a signed-in password change.

    Deliberately carries no secret and no one-click undo: the only useful
    action for someone who did not make the change is a fresh reset, which
    proves control of this inbox.
    """
    subject = f"Your {settings.BRAND_NAME} password was changed"

    text = (
        f"Hi {name},\n\n"
        f"The password on your {settings.BRAND_NAME} account was just changed, and any "
        "other devices signed in to it were signed out.\n\n"
        "If this was you, there is nothing else to do.\n\n"
        "If it was not you, reset your password now:\n"
        f"{reset_url}\n\n"
        f"Then write to {settings.SUPPORT_EMAIL} so we can check the account.\n\n"
        f"— {settings.BRAND_NAME} | {settings.BUSINESS_NAME}"
    )

    html = render(
        preheader="Your password was changed. Other devices were signed out.",
        eyebrow="Security notice",
        heading="Your password was changed",
        paragraphs=[
            f"Hi {escape(name)}, the password on your {escape(settings.BRAND_NAME)} account "
            "was just changed, and any other devices signed in to it were signed out.",
            "If this was you, there is nothing else to do.",
            "If it was not you, reset your password straight away using the button below, "
            f"then write to {escape(settings.SUPPORT_EMAIL)} so the account can be checked.",
        ],
        cta_label="Reset my password",
        cta_url=reset_url,
        footnote="You are receiving this because it is a security change on your account.",
    )
    return subject, text, html


def coach_new_lead(
    name: str, email: str, goal: str | None, phone: str | None = None
) -> tuple[str, str, str]:
    subject = f"New enquiry from {name}"
    goal_text = goal or "not given"
    phone_text = phone or "not given"

    text = (
        "A new enquiry came in through the website.\n\n"
        f"Name:  {name}\n"
        f"Email: {email}\n"
        f"Phone: {phone_text}\n"
        f"Goal:  {goal_text}\n\n"
        f"Open the dashboard: {settings.FRONTEND_URL.rstrip('/')}\n\n"
        f"— {settings.BRAND_NAME}"
    )

    html = render(
        preheader=f"{name} — {goal_text}",
        eyebrow="New enquiry",
        heading=escape(name),
        paragraphs=[
            "A new enquiry came in through the website.",
            f"<strong style=\"color:{CHALK_50};\">Email</strong> "
            f'<a href="mailto:{escape(email, quote=True)}">{escape(email)}</a><br>'
            f"<strong style=\"color:{CHALK_50};\">Phone</strong> {escape(phone_text)}<br>"
            f"<strong style=\"color:{CHALK_50};\">Goal</strong> {escape(goal_text)}",
        ],
        cta_label="Reply to this enquiry",
        cta_url=f"mailto:{email}",
        footnote="Enquiries answered within one business day convert roughly twice as often.",
    )
    return subject, text, html


def payment_failed(
    name: str,
    amount: str,
    retry_text: str,
    reason: str | None,
    update_url: str,
) -> tuple[str, str, str]:
    """The most valuable email this system sends.

    A failed renewal is almost never a decision — it is an expired card, a new
    bank, a travel block. The subscription is lost only if nobody tells the
    person in time. So this email does three things and nothing else: says what
    failed, says when the last automatic retry happens, and links straight to
    the card form. No marketing, no apology paragraph, one button.
    """
    subject = f"Action needed: your {settings.BRAND_NAME} payment did not go through"

    text = (
        f"Hi {name},\n\n"
        f"We could not take the {amount} payment for your {settings.BRAND_NAME} plan.\n"
        + (f"Reason given by the bank: {reason}\n" if reason else "")
        + f"\n{retry_text}\n\n"
        "Your coaching is still active in the meantime — nothing has been switched off.\n\n"
        f"Update your card here:\n{update_url}\n\n"
        f"— {settings.BRAND_NAME} | {settings.BUSINESS_NAME}"
    )

    paragraphs = [
        f"We could not take the <strong style=\"color:{CHALK_50};\">{escape(amount)}</strong> "
        f"payment for your {escape(settings.BRAND_NAME)} plan.",
    ]
    if reason:
        paragraphs.append(f"Your bank said: {escape(reason)}")
    paragraphs.append(escape(retry_text))
    paragraphs.append(
        "Your coaching is still active while this is sorted out — nothing has been "
        "switched off, and no training has been lost."
    )

    html = render(
        preheader=f"We could not take your {amount} payment. Update your card to keep training.",
        eyebrow="Payment problem",
        heading="Your payment did not go through",
        paragraphs=paragraphs,
        cta_label="Update your card",
        cta_url=update_url,
        footnote=(
            "Nine times out of ten this is an expired card rather than anything wrong. "
            "It takes about a minute to fix."
        ),
    )
    return subject, text, html


def subscription_cancelled(name: str, program_name: str, resubscribe_url: str) -> tuple[str, str, str]:
    """Sent when the subscription actually ends, not when cancellation is requested.

    The gap matters. Cancelling schedules an ending; this confirms it has
    happened. Sending it at request time would tell someone their coaching had
    stopped three weeks before it did.
    """
    subject = f"Your {settings.BRAND_NAME} plan has ended"

    text = (
        f"Hi {name},\n\n"
        f"Your {program_name} plan has now ended and you will not be charged again.\n\n"
        "Your logged training, weights and check-in photos are all still on your account. "
        "Nothing has been deleted.\n\n"
        f"If you want to pick things back up:\n{resubscribe_url}\n\n"
        f"— {settings.BRAND_NAME} | {settings.BUSINESS_NAME}"
    )

    html = render(
        preheader="Your plan has ended. Your training history is still here.",
        eyebrow="Plan ended",
        heading=f"Thanks for training with us, {escape(name)}.",
        paragraphs=[
            f"Your {escape(program_name)} plan has ended and you will not be charged again.",
            "Everything you logged — sessions, weights, measurements, check-in photos — is "
            "still on your account and is not going anywhere.",
        ],
        cta_label="Start again",
        cta_url=resubscribe_url,
        footnote=(
            "If something about the coaching did not work for you, replying to this email "
            "reaches the coach directly. It is read."
        ),
    )
    return subject, text, html
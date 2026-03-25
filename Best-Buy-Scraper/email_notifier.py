import os
import smtplib
from email.message import EmailMessage

# -----------------------------------------------------------------------------
# MODULE OVERVIEW
# -----------------------------------------------------------------------------
# This module centralizes email delivery for scraper alerts and completion
# reports. Other modules call `notify_admin(...)` and do not need to manage SMTP
# sessions directly.

def notify_admin(subject: str, body: str, to_addr: str = None, attachments: list[str] = None):
    """Send alert/report emails using SMTP settings from environment variables.

    Resolution order:
    - Recipient: explicit `to_addr` argument, else EMAIL_TO env
    - Sender: EMAIL_FROM env, else EMAIL_USERNAME, else recipient
    """
    # Read SMTP + addressing config from environment.
    # These values are intentionally read per-call so runtime env changes are
    # picked up without restarting the Python process.
    to_addr = to_addr or os.environ.get("EMAIL_TO", "sania@behope.com")
    smtp_host = os.environ.get("EMAIL_SMTP_HOST")
    smtp_port = int(os.environ.get("EMAIL_SMTP_PORT", "0") or 0)
    username = os.environ.get("EMAIL_USERNAME")
    password = os.environ.get("EMAIL_PASSWORD")
    from_addr = os.environ.get("EMAIL_FROM") or username or to_addr

    # Build email object once; transport method (SSL/TLS/plain) is chosen later.
    # The same message object is passed to send_message in either SMTP mode.
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    # --- Attach files (no mimetype detection) ---
    # Attachments are sent as generic octet-stream to keep this utility simple
    # and robust across arbitrary file types.
    if attachments:
        for file_path in attachments:
            # Skip missing paths rather than failing entire notification flow.
            if not os.path.isfile(file_path):
                print(f"⚠️  Skipping missing attachment: {file_path}")
                continue
            with open(file_path, "rb") as f:
                msg.add_attachment(
                    f.read(),
                    maintype="application",
                    subtype="octet-stream",
                    filename=os.path.basename(file_path)
                )

    # Fallback mode: if SMTP config is missing, print details instead of raising.
    # This keeps scraper execution observable even in misconfigured environments.
    if not smtp_host or not smtp_port:
        print("email_notifier: SMTP host/port not set; cannot send email. Printing details instead.")
        print("Subject:", subject)
        print("To:", to_addr)
        print(body)
        if attachments:
            print("Attachments:", attachments)
        return False

    try:
        # Port 465 uses implicit SSL; other ports attempt STARTTLS upgrade.
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as server:
                # Auth is optional for SMTP relays that trust source network.
                if username and password:
                    server.login(username, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                server.ehlo()
                try:
                    # Upgrade plain connection to encrypted channel when supported.
                    server.starttls()
                    server.ehlo()
                except Exception:
                    # Some SMTP endpoints do not support STARTTLS; continue plain.
                    pass
                if username and password:
                    server.login(username, password)
                server.send_message(msg)
        print(f"✅ Email sent to {to_addr} (subject: {subject})")
        return True
    except Exception as e:
        print("email_notifier: Failed to send email:", e)
        return False
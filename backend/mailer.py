"""mailer.py — Email dispatcher for BESCOM SCADA Two-Factor Authentication.

Supports real SMTP dispatch when configured via environment variables:
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS, SMTP_FROM
In local/prototype mode without SMTP configured, cleanly logs the OTP to the
terminal and returns simulation metadata so the UI prototype can preview it.
"""
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

def send_otp_email(recipient_email, otp_code, ttl_minutes=5):
    recipient_email = (recipient_email or "").strip()
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", 587))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    smtp_from = os.environ.get("SMTP_FROM", "bescom-scada-alerts@karnataka.gov.in")
    
    subject = f"[BESCOM SCADA] One-Time Verification Passcode: {otp_code}"
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>BESCOM SCADA Authentication</title>
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #0B1120; color: #E2E8F0; padding: 24px; margin: 0;">
      <div style="max-width: 520px; margin: 0 auto; background-color: #1E293B; border: 1px solid #334155; border-radius: 12px; overflow: hidden; box-shadow: 0 10px 25px rgba(0,0,0,0.5);">
        <div style="background: linear-gradient(90deg, #0F172A, #1E293B); padding: 20px; border-bottom: 1px solid #334155; text-align: center;">
          <h2 style="margin: 0; color: #38BDF8; font-size: 20px; font-weight: 700;">BESCOM SCADA CONTROL ROOM</h2>
          <p style="margin: 4px 0 0; color: #94A3B8; font-size: 12px; letter-spacing: 0.5px;">BENGALURU ELECTRICITY SUPPLY COMPANY LIMITED &bull; CIRCLE 4</p>
        </div>
        <div style="padding: 28px 24px;">
          <p style="margin: 0 0 16px; font-size: 14px; color: #CBD5E1;">
            An operator sign-in attempt was initiated for user <strong>{recipient_email}</strong>.
          </p>
          <p style="margin: 0 0 20px; font-size: 13px; color: #94A3B8;">
            Please enter the following 6-digit One-Time Passcode (OTP) on the verification screen to access the Virtual Distribution Grid Control Room:
          </p>
          <div style="background-color: #0F1A2E; border: 2px dashed #06B6D4; border-radius: 8px; padding: 18px; text-align: center; margin: 20px 0;">
            <div style="font-size: 11px; text-transform: uppercase; color: #64748B; letter-spacing: 1.5px; font-weight: 600; margin-bottom: 6px;">Authentication Code</div>
            <div style="font-size: 34px; font-family: 'JetBrains Mono', monospace, Courier; font-weight: 800; letter-spacing: 8px; color: #10B981;">{otp_code}</div>
          </div>
          <p style="margin: 16px 0 0; font-size: 12px; color: #F59E0B;">
            &bull; This code is strictly confidential and expires in <strong>{ttl_minutes} minutes</strong>.
          </p>
          <p style="margin: 8px 0 0; font-size: 12px; color: #64748B;">
            If you did not request this code, an unauthorized sign-in was attempted. Please notify the BESCOM SCADA Cyber Security Division.
          </p>
        </div>
        <div style="background-color: #0F172A; padding: 14px; text-align: center; border-top: 1px solid #334155; font-size: 11px; color: #475569;">
          &copy; BESCOM SCADA System &bull; Theme 9 Fault-to-Recovery Architecture
        </div>
      </div>
    </body>
    </html>
    """

    # Always log to stdout with high visibility
    print("\n" + "=" * 64)
    print(" [BESCOM SCADA] TWO-FACTOR AUTHENTICATION (2FA) OTP DISPATCHED")
    print(f" Recipient: {recipient_email}")
    print(f" OTP Code:  {otp_code}")
    print(f" Validity:  {ttl_minutes} minutes")
    print("=" * 64 + "\n")

    if smtp_host and smtp_user and smtp_pass:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = smtp_from
            msg["To"] = recipient_email
            msg.attach(MIMEText(f"Your BESCOM SCADA OTP code is: {otp_code}. Valid for {ttl_minutes} minutes.", "plain"))
            msg.attach(MIMEText(html_content, "html"))
            
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            print(f"[SMTP] Successfully dispatched OTP email to {recipient_email}")
            return {"sent": True, "method": "smtp", "debug_otp": otp_code}
        except Exception as e:
            print(f"[SMTP WARNING] Failed to send email via SMTP ({e}). Prototype fallback active.")
            return {"sent": False, "method": "simulated", "error": str(e), "debug_otp": otp_code}
    
    return {"sent": True, "method": "simulated", "debug_otp": otp_code}

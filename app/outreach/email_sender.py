import smtplib
from html import escape
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import current_app


def send_email(to_address, subject, body, reply_to=None):
    """Send an email via SMTP."""
    smtp_server = current_app.config.get('SMTP_SERVER')
    smtp_port = current_app.config.get('SMTP_PORT', 587)
    username = current_app.config.get('SMTP_USERNAME')
    password = current_app.config.get('SMTP_PASSWORD')
    sender_name = current_app.config.get('SENDER_NAME', 'Super Lawyer Reachout')

    if not all([smtp_server, username, password]):
        raise ValueError("SMTP not configured. Set SMTP_SERVER, SMTP_USERNAME, SMTP_PASSWORD.")

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = f"{sender_name} <{username}>"
    msg['To'] = to_address
    if reply_to:
        msg['Reply-To'] = reply_to

    # Plain text
    msg.attach(MIMEText(body, 'plain'))

    # Simple HTML version — escape special chars before converting newlines
    html_body = escape(body).replace('\n', '<br>')
    html = f"""<html><body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
{html_body}
</body></html>"""
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(username, password)
        server.send_message(msg)

    return True

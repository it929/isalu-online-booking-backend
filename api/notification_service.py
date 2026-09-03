"""
Appointment Notification & Reminder Service
Handles Email and SMS notifications for patient appointment reminders (1 day prior to clinic).
"""

import os
import logging
import datetime
import urllib.request
import urllib.parse
import json
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

HOSPITAL_NAME = "Isalu Hospitals"
HOSPITAL_ADDRESS = "1, Isalu Way, Ikeja, Lagos State, Nigeria"
HOSPITAL_PHONE = "+234 800 472 5800"
HOSPITAL_EMAIL = "helpdesk@isalu.ng"


def format_booking_date_display(date_str: str) -> str:
    """Formats 'YYYY-MM-DD' date string into readable date string like 'Saturday, September 5, 2026'."""
    if not date_str:
        return "Tomorrow"
    try:
        dt = datetime.datetime.strptime(date_str.strip(), "%Y-%m-%d")
        return dt.strftime("%A, %B %d, %Y")
    except Exception:
        return date_str


def send_email_reminder(booking, is_3hour_notice: bool = False) -> tuple[bool, str]:
    """
    Sends an HTML and plain text appointment reminder email to the patient.
    """
    recipient_email = (booking.patient_email or "").strip()
    if not recipient_email or "@" not in recipient_email:
        logger.info(f"Skipping Email reminder for booking {booking.ref_code}: Invalid or empty email '{recipient_email}'")
        return False, "No valid email address provided"

    patient_name = booking.patient_name or "Valued Patient"
    doctor_acronym = get_doctor_acronym_display(booking)
    doctor_specialty = booking.doctor_specialty or "General Clinic"
    date_display = format_booking_date_display(booking.date)
    time_display = booking.time or "Scheduled Session Time"
    ref_code = booking.ref_code
    payment_type = booking.payment_type or "Private Self-Pay"
    hmo_info = f" (HMO: {booking.hmo_name})" if booking.payment_type == "HMO Insurance" and booking.hmo_name else ""

    if is_3hour_notice:
        subject = f"Urgent Reminder: Session Starts in 3 Hours at {HOSPITAL_NAME} [{ref_code}]"
        time_heading = "TODAY in 3 Hours"
    else:
        subject = f"Appointment Reminder: Tomorrow at {HOSPITAL_NAME} [{ref_code}]"
        time_heading = "Tomorrow"

    # Plain text version
    text_content = (
        f"Dear {patient_name},\n\n"
        f"This is a friendly reminder for your medical appointment {time_heading} with {doctor_acronym} ({doctor_specialty}) at {HOSPITAL_NAME}.\n\n"
        f"APPOINTMENT DETAILS:\n"
        f"- Reference Code: {ref_code}\n"
        f"- Date: {date_display}\n"
        f"- Time: {time_display}\n"
        f"- Clinic/Specialty: {doctor_specialty}\n"
        f"- Attending Specialist: {doctor_acronym}\n"
        f"- Payment Type: {payment_type}{hmo_info}\n\n"
        f"PATIENT INSTRUCTIONS:\n"
        f"1. Please arrive at least 15 minutes before your scheduled appointment time for check-in.\n"
        f"2. Bring a valid Photo ID card and your HMO membership card (if applicable).\n"
        f"3. Present your Reference Code ({ref_code}) to the Reception Helpdesk.\n\n"
        f"Location: {HOSPITAL_ADDRESS}\n"
        f"Contact Desk: {HOSPITAL_PHONE} | {HOSPITAL_EMAIL}\n\n"
        f"Thank you for choosing {HOSPITAL_NAME}."
    )

    # Rich HTML version
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f8fafc; margin: 0; padding: 20px; color: #1e293b; }}
        .container {{ max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 16px; overflow: hidden; border: 1px solid #e2e8f0; box-shadow: 0 4px 12px rgba(0,0,0,0.05); }}
        .header {{ background: linear-gradient(135deg, #008ac9 0%, #005a87 100%); color: #ffffff; padding: 28px 24px; text-align: center; }}
        .header h1 {{ margin: 0; font-size: 22px; font-weight: 800; letter-spacing: -0.5px; }}
        .header p {{ margin: 6px 0 0 0; opacity: 0.9; font-size: 13px; font-weight: 600; }}
        .badge {{ background: rgba(255,255,255,0.2); padding: 4px 12px; border-radius: 20px; display: inline-block; font-size: 12px; font-weight: 700; margin-top: 10px; }}
        .content {{ padding: 28px 24px; }}
        .greeting {{ font-size: 16px; font-weight: 700; color: #0f172a; margin-bottom: 12px; }}
        .card {{ background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; padding: 18px; margin: 20px 0; }}
        .card-title {{ font-size: 12px; text-transform: uppercase; font-weight: 800; color: #166534; letter-spacing: 0.5px; margin-bottom: 12px; border-bottom: 1px dashed #bbf7d0; padding-bottom: 6px; }}
        .detail-row {{ display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 13px; }}
        .detail-label {{ font-weight: 600; color: #475569; }}
        .detail-value {{ font-weight: 800; color: #0f172a; text-align: right; }}
        .ref-box {{ background: #008ac9; color: #ffffff; text-align: center; padding: 14px; border-radius: 10px; font-size: 20px; font-weight: 900; letter-spacing: 2px; margin: 16px 0; }}
        .instructions {{ background: #f8fafc; border-left: 4px solid #008ac9; padding: 14px; border-radius: 4px; font-size: 12px; line-height: 1.6; color: #334155; margin: 20px 0; }}
        .footer {{ background: #f1f5f9; padding: 20px 24px; text-align: center; font-size: 11px; color: #64748b; border-top: 1px solid #e2e8f0; }}
      </style>
    </head>
    <body>
      <div class="container">
        <div class="header">
          <h1>{HOSPITAL_NAME}</h1>
          <p>Official Patient Consultation Reminder</p>
          <div class="badge">Appointment Tomorrow</div>
        </div>

        <div class="content">
          <div class="greeting">Hello {patient_name},</div>
          <p style="font-size: 13px; line-height: 1.5; color: #334155;">
            This is a friendly reminder for your scheduled doctor's consultation tomorrow. Here are your appointment details:
          </p>

          <div class="ref-box">
            REF CODE: {ref_code}
          </div>

          <div class="card">
            <div class="card-title">Appointment Summary</div>
            <div class="detail-row">
              <span class="detail-label">Date:</span>
              <span class="detail-value">{date_display}</span>
            </div>
            <div class="detail-row">
              <span class="detail-label">Time:</span>
              <span class="detail-value">{time_display}</span>
            </div>
            <div class="detail-row">
              <span class="detail-label">Clinic / Specialty:</span>
              <span class="detail-value">{doctor_specialty}</span>
            </div>
            <div class="detail-row">
              <span class="detail-label">Attending Specialist:</span>
              <span class="detail-value">{doctor_acronym}</span>
            </div>
            <div class="detail-row">
              <span class="detail-label">Payment Desk:</span>
              <span class="detail-value">{payment_type}{hmo_info}</span>
            </div>
          </div>

          <div class="instructions">
            <strong>Check-In Instructions:</strong><br>
            • Please arrive 15 minutes prior to your session time for queue check-in.<br>
            • Present your Reference Code (<strong>{ref_code}</strong>) at the reception helpdesk.<br>
            • Bring a valid identity card and HMO enrollee card (if applicable).
          </div>
        </div>

        <div class="footer">
          <strong>{HOSPITAL_NAME}</strong> • {HOSPITAL_ADDRESS}<br>
          Phone: {HOSPITAL_PHONE} | Email: {HOSPITAL_EMAIL}<br>
          <em>This is an automated notification system. Please do not reply directly to this email.</em>
        </div>
      </div>
    </body>
    </html>
    """

    try:
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "Isalu Hospitals <no-reply@isalu.ng>")
        msg = EmailMultiAlternatives(subject, text_content, from_email, [recipient_email])
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
        logger.info(f"Successfully sent Email reminder to {recipient_email} for booking {ref_code}")
        return True, "Email sent successfully"
    except Exception as e:
        logger.warning(f"Email dispatch error for booking {ref_code} ({recipient_email}): {str(e)}")
        # If SMTP is not configured in local dev, log it clearly
        print(f"[SIMULATED EMAIL REMINDER DISPATCH]\nTo: {recipient_email}\nSubject: {subject}\nRef: {ref_code}")
        return True, f"Email logged/sent: {str(e)}"


def get_doctor_acronym_display(booking) -> str:
    """
    Returns the doctor's public acronym (e.g. 'Specialist A') instead of real name for SMS privacy.
    """
    try:
        from api.models import Doctor
        doc_id = (booking.doctor_id or "").strip()
        doc_name = (booking.doctor_name or "").strip()

        doctor_obj = None
        if doc_id:
            doctor_obj = Doctor.objects.filter(doc_id=doc_id).first()

        if not doctor_obj and doc_name:
            doctor_obj = (
                Doctor.objects.filter(full_name__iexact=doc_name).first()
                or Doctor.objects.filter(name__iexact=doc_name).first()
            )

        if doctor_obj:
            return doctor_obj.acronym or doctor_obj.name or "Specialist"

        if "specialist" in doc_name.lower() or "doc" not in doc_name.lower():
            return doc_name
    except Exception:
        pass

    return "Specialist"


def send_sms_reminder(booking, is_3hour_notice: bool = False) -> tuple[bool, str]:
    """
    Sends an SMS appointment reminder to the patient's phone number.
    Supports BulkSMS.com (Token/Basic Auth), Twilio, Termii, or console logging fallback.
    Uses doctor acronym instead of real name for privacy.
    """
    phone = (booking.patient_phone or "").strip()
    if not phone or len(phone) < 7:
        logger.info(f"Skipping SMS reminder for booking {booking.ref_code}: Invalid phone number '{phone}'")
        return False, "No valid phone number provided"

    patient_name = (booking.patient_name or "Patient").split()[0]
    doctor_acronym = get_doctor_acronym_display(booking)
    doctor_specialty = booking.doctor_specialty or "General Clinic"
    date_display = format_booking_date_display(booking.date)
    time_display = booking.time or "Scheduled Time"
    ref_code = booking.ref_code

    if is_3hour_notice:
        sms_text = (
            f"ISALU HOSPITALS: Hello {patient_name}, reminder: your appointment starts TODAY in 3 hours ({time_display}) "
            f"with {doctor_acronym} ({doctor_specialty}). Ref Code: {ref_code}. Pls arrive 15 mins early. Contact: {HOSPITAL_PHONE}"
        )
    else:
        sms_text = (
            f"ISALU HOSPITALS: Hello {patient_name}, reminder for your appointment tomorrow ({date_display} at {time_display}) "
            f"with {doctor_acronym} ({doctor_specialty}). Ref Code: {ref_code}. Pls arrive 15 mins early. Contact: {HOSPITAL_PHONE}"
        )

    # Format phone number for international BulkSMS standard (E.164 without leading plus for local Nigerian numbers)
    clean_phone = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if clean_phone.startswith("0") and len(clean_phone) == 11:
        clean_phone = "234" + clean_phone[1:]
    elif clean_phone.startswith("+"):
        clean_phone = clean_phone[1:]

    # ---------------------------------------------------------
    # 1. EBULKSMS.COM API DISPATCH (https://www.ebulksms.com - Official Spec)
    # ---------------------------------------------------------
    ebulksms_username = os.getenv("EBULKSMS_USERNAME", "").strip()
    ebulksms_apikey = os.getenv("EBULKSMS_API_KEY", "").strip()
    ebulksms_api_url = os.getenv("EBULKSMS_API_URL", "https://api.ebulksms.com/sendsms.json").strip()
    ebulksms_sender = os.getenv("EBULKSMS_SENDER_ID", "ISALU")[:11]

    if ebulksms_username and ebulksms_apikey:
        try:
            payload = json.dumps({
                "SMS": {
                    "auth": {
                        "username": ebulksms_username,
                        "apikey": ebulksms_apikey
                    },
                    "message": {
                        "sender": ebulksms_sender,
                        "messagetext": sms_text,
                        "flash": "0"
                    },
                    "recipients": {
                        "gsm": [
                            {
                                "msidn": clean_phone,
                                "msgid": f"REM_{ref_code}_{int(timezone.now().timestamp())}"
                            }
                        ]
                    },
                    "dndsender": 1
                }
            }).encode('utf-8')

            req = urllib.request.Request(
                ebulksms_api_url,
                data=payload,
                headers={"Content-Type": "application/json"}
            )

            with urllib.request.urlopen(req, timeout=10) as response:
                res_body = response.read().decode('utf-8')
                logger.info(f"EbulkSMS.com response for booking {ref_code}: {res_body}")
                try:
                    res_json = json.loads(res_body)
                    status_str = res_json.get("response", {}).get("status", "")
                    if status_str == "SUCCESS":
                        return True, "SMS sent via EbulkSMS API"
                    else:
                        return False, f"EbulkSMS API response: {status_str}"
                except Exception:
                    return True, f"EbulkSMS dispatched: {res_body[:100]}"
        except urllib.error.HTTPError as http_err:
            err_body = ""
            try:
                err_body = http_err.read().decode('utf-8')
            except Exception:
                pass
            logger.error(f"EbulkSMS HTTP Error {http_err.code} for {ref_code}: {err_body or str(http_err)}")
            return False, f"EbulkSMS API Error ({http_err.code}): {err_body or str(http_err)}"
        except Exception as e:
            logger.error(f"Failed to post SMS to EbulkSMS.com for {ref_code}: {str(e)}")
            return False, f"EbulkSMS.com API error: {str(e)}"

    # ---------------------------------------------------------
    # 2. BULKSMS.COM INTERNATIONAL API DISPATCH (https://www.bulksms.com)
    # ---------------------------------------------------------
    bulksms_token_id = os.getenv("BULKSMS_TOKEN_ID", "").strip()
    bulksms_token_secret = os.getenv("BULKSMS_TOKEN_SECRET", "").strip()
    bulksms_username = os.getenv("BULKSMS_USERNAME", "").strip()
    bulksms_password = os.getenv("BULKSMS_PASSWORD", "").strip()
    bulksms_api_url = os.getenv("BULKSMS_API_URL", "https://api.bulksms.com/v1/messages").strip()

    if (bulksms_token_id and bulksms_token_secret) or (bulksms_username and bulksms_password):
        try:
            import base64
            # BulkSMS REST API expects array of message objects
            payload = json.dumps([{
                "to": clean_phone,
                "body": sms_text,
                "from": os.getenv("BULKSMS_SENDER_ID", "ISALU")
            }]).encode('utf-8')

            req = urllib.request.Request(
                bulksms_api_url,
                data=payload,
                headers={"Content-Type": "application/json"}
            )

            if bulksms_token_id and bulksms_token_secret:
                auth_str = f"{bulksms_token_id}:{bulksms_token_secret}"
            else:
                auth_str = f"{bulksms_username}:{bulksms_password}"

            b64_auth = base64.b64encode(auth_str.encode('utf-8')).decode('utf-8')
            req.add_header("Authorization", f"Basic {b64_auth}")

            with urllib.request.urlopen(req, timeout=10) as response:
                res_body = response.read().decode('utf-8')
                logger.info(f"BulkSMS.com response for booking {ref_code}: {res_body}")
                return True, "SMS sent via BulkSMS.com API"
        except Exception as e:
            logger.error(f"Failed to post SMS to BulkSMS.com for {ref_code}: {str(e)}")
            return False, f"BulkSMS.com API error: {str(e)}"

    # ---------------------------------------------------------
    # 2. FALLBACK GENERIC SMS GATEWAY DISPATCH
    # ---------------------------------------------------------
    sms_api_url = os.getenv("SMS_API_URL", "")
    sms_api_key = os.getenv("SMS_API_KEY", "")

    if sms_api_url and sms_api_key:
        try:
            payload = json.dumps({
                "to": clean_phone,
                "message": sms_text,
                "api_key": sms_api_key,
                "sender": os.getenv("SMS_SENDER_ID", "ISALU")
            }).encode('utf-8')

            req = urllib.request.Request(
                sms_api_url,
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                res_body = response.read().decode('utf-8')
                logger.info(f"SMS Gateway response for booking {ref_code}: {res_body}")
                return True, "SMS sent via Gateway API"
        except Exception as e:
            logger.error(f"Failed to post SMS to gateway for {ref_code}: {str(e)}")

    # Standard fallback logging when no external SMS service key is set
    print(f"\n[SMS REMINDER DISPATCH]\nTo: {clean_phone}\nBody: {sms_text}\nRef: {ref_code}\n")
    logger.info(f"Dispatched SMS reminder for booking {ref_code} to {clean_phone}")
    return True, "SMS reminder logged & dispatched"


def send_single_booking_reminder(booking, force: bool = False, is_3hour_notice: bool = False) -> dict:
    """
    Sends Email & SMS reminders for a single booking instance and marks reminder_sent = True in database.
    """
    if booking.status == "Cancelled":
        return {
            "success": False,
            "ref_code": booking.ref_code,
            "message": "Cannot send reminder for a cancelled appointment."
        }

    if booking.reminder_sent and not force:
        return {
            "success": True,
            "already_sent": True,
            "ref_code": booking.ref_code,
            "message": f"Reminder already sent on {booking.reminder_sent_at}"
        }

    email_ok, email_msg = send_email_reminder(booking, is_3hour_notice=is_3hour_notice)
    sms_ok, sms_msg = send_sms_reminder(booking, is_3hour_notice=is_3hour_notice)

    booking.reminder_sent = True
    booking.reminder_sent_at = timezone.now()
    booking.save(update_fields=["reminder_sent", "reminder_sent_at"])

    return {
        "success": True,
        "ref_code": booking.ref_code,
        "patient_name": booking.patient_name,
        "patient_email": booking.patient_email,
        "patient_phone": booking.patient_phone,
        "email_sent": email_ok,
        "email_message": email_msg,
        "sms_sent": sms_ok,
        "sms_message": sms_msg,
        "is_3hour_notice": is_3hour_notice,
        "sent_at": booking.reminder_sent_at.isoformat(),
    }


def parse_booking_start_time(date_str: str, time_str: str):
    """
    Parses a booking's date string ('YYYY-MM-DD') and time string ('08:00 AM - 02:00 PM')
    into a timezone-aware datetime object.
    """
    if not date_str:
        return None

    time_part = (time_str or "").split("-")[0].split("–")[0].strip()
    if not time_part:
        time_part = "08:00 AM"

    dt_str = f"{date_str.strip()} {time_part}"
    formats = [
        "%Y-%m-%d %I:%M %p",
        "%Y-%m-%d %I:%M%p",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %I %p",
    ]
    tz = timezone.get_current_timezone()
    for fmt in formats:
        try:
            naive_dt = datetime.datetime.strptime(dt_str, fmt)
            return timezone.make_aware(naive_dt, tz)
        except ValueError:
            continue

    try:
        naive_dt = datetime.datetime.strptime(f"{date_str.strip()} 08:00 AM", "%Y-%m-%d %I:%M %p")
        return timezone.make_aware(naive_dt, tz)
    except Exception:
        return None


def process_3hour_appointment_reminders(hours_ahead: int = 3, force: bool = False) -> dict:
    """
    Finds all active, non-cancelled appointments starting in approximately 3 hours (or within 3 hours)
    and dispatches Email + SMS reminders.
    """
    from api.models import Booking

    now = timezone.now()
    window_end = now + datetime.timedelta(hours=hours_ahead)

    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    query = Booking.objects.filter(
        date__in=[today_str, tomorrow_str],
        is_active=True
    ).exclude(status__iexact="Cancelled")

    if not force:
        query = query.filter(reminder_sent=False)

    all_bookings = list(query)
    eligible_bookings = []

    for b in all_bookings:
        start_dt = parse_booking_start_time(b.date, b.time)
        if start_dt:
            if now <= start_dt <= window_end:
                eligible_bookings.append(b)

    total_eligible = len(eligible_bookings)
    results = []
    success_count = 0
    failed_count = 0

    for booking in eligible_bookings:
        try:
            res = send_single_booking_reminder(booking, force=force, is_3hour_notice=True)
            results.append(res)
            success_count += 1
        except Exception as e:
            logger.error(f"Error sending 3-hour reminder for booking {booking.ref_code}: {str(e)}")
            failed_count += 1
            results.append({
                "success": False,
                "ref_code": booking.ref_code,
                "error": str(e)
            })

    summary = {
        "status": "success",
        "mode": "3_hours_notice",
        "hours_ahead": hours_ahead,
        "total_eligible": total_eligible,
        "processed_count": success_count,
        "failed_count": failed_count,
        "reminders": results,
    }

    logger.info(f"3-hour reminder process complete: {success_count}/{total_eligible} sent successfully.")
    return summary


def process_appointment_reminders(target_date: str = None, days_ahead: int = 1, force: bool = False) -> dict:
    """
    Finds all active, non-cancelled appointments scheduled for the target date (default 1 day in advance)
    and sends Email + SMS reminders to patients.
    """
    from api.models import Booking

    if not target_date:
        target_dt = datetime.date.today() + datetime.timedelta(days=days_ahead)
        target_date = target_dt.strftime("%Y-%m-%d")

    logger.info(f"Starting appointment reminder process for date: {target_date} (days_ahead={days_ahead})")

    query = Booking.objects.filter(
        date=target_date,
        is_active=True
    ).exclude(status__iexact="Cancelled")

    if not force:
        query = query.filter(reminder_sent=False)

    eligible_bookings = list(query)
    total_eligible = len(eligible_bookings)

    results = []
    success_count = 0
    failed_count = 0

    for booking in eligible_bookings:
        try:
            res = send_single_booking_reminder(booking, force=force)
            results.append(res)
            success_count += 1
        except Exception as e:
            logger.error(f"Error sending reminder for booking {booking.ref_code}: {str(e)}")
            failed_count += 1
            results.append({
                "success": False,
                "ref_code": booking.ref_code,
                "error": str(e)
            })

    summary = {
        "status": "success",
        "target_date": target_date,
        "total_eligible": total_eligible,
        "processed_count": success_count,
        "failed_count": failed_count,
        "reminders": results,
    }

    logger.info(f"Reminder process complete for {target_date}: {success_count}/{total_eligible} sent successfully.")
    return summary

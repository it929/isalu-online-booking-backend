from django.apps import AppConfig

class ApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'api'
    verbose_name = 'Isalu Hospitals Medical API'

    def ready(self):
        import os, threading, time
        # Ensure thread runs only in main server process to avoid duplicates in reloader
        if os.environ.get('RUN_MAIN') == 'true' or not os.environ.get('SERVER_SOFTWARE'):
            def run_periodic_3hour_reminders():
                time.sleep(15)  # 15s grace period after app startup
                while True:
                    try:
                        from api.notification_service import process_3hour_appointment_reminders
                        process_3hour_appointment_reminders(hours_ahead=3)
                    except Exception:
                        pass
                    time.sleep(900)  # Check every 15 minutes

            t = threading.Thread(target=run_periodic_3hour_reminders, daemon=True)
            t.start()

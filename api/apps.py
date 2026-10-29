from django.apps import AppConfig

class ApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'api'
    verbose_name = 'Isalu Hospitals Medical API'

    def ready(self):
        import os, threading, time
        # SQLite: start every transaction with BEGIN IMMEDIATE so two writers
        # queue (up to the database timeout) instead of deadlocking with
        # "database is locked" when one of them read first. Django 5.1+ has this
        # as OPTIONS["transaction_mode"]; this project runs on Django < 5.1.
        if os.environ.get("SQLITE_BEGIN_IMMEDIATE", "true").lower() in ("1", "true", "yes"):
            from django.db.backends.sqlite3.base import DatabaseWrapper

            def _begin_immediate(self):
                self.cursor().execute("BEGIN IMMEDIATE")

            DatabaseWrapper._start_transaction_under_autocommit = _begin_immediate

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

        # Automatic payment check (Payment Reconciliation): only in the web server
        # process, never in manage.py commands like migrate. One run at a time
        # across processes (database lock).
        import sys
        argv = " ".join(sys.argv).lower()
        serving = "runserver" in argv or any(x in argv for x in ("daphne", "gunicorn", "uvicorn")) \
            or os.environ.get("PAYMENT_SWEEP_IN_PROCESS", "").lower() in ("1", "true", "yes")
        if serving and (os.environ.get('RUN_MAIN') == 'true' or "--noreload" in argv or "runserver" not in argv):
            def run_payment_sweep():
                from django.conf import settings
                minutes = getattr(settings, "PAYMENT_SWEEP_MINUTES", 15)
                if not minutes:
                    return
                time.sleep(90)
                while True:
                    try:
                        from api.payment_ops import run_sweep
                        run_sweep()
                    except Exception:
                        import logging
                        logging.getLogger(__name__).exception("Automatic payment check failed")
                    time.sleep(minutes * 60)

            threading.Thread(target=run_payment_sweep, daemon=True).start()

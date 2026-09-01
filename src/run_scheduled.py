import os
import sys
import time

try:
    import dotenv
except ImportError:
    dotenv = None

if not os.environ.get("IS_DOCKER", False):
    if dotenv is not None:
        dotenv.load_dotenv()
else:
    for key, value in os.environ.items():
        if isinstance(value, str):
            os.environ[key] = value.strip('\'"\r\n')


def safe_run_job(job_func, *args, **kwargs):
    """
    Executes a scheduled job safely, catching unhandled exceptions
    so that the scheduled update loop does not crash the process.
    """
    try:
        job_func(*args, **kwargs)
    except Exception as e:
        print(f"[SCHEDULE ERROR] Job '{getattr(job_func, '__name__', str(job_func))}' failed with error: {e}", file=sys.stderr)


def run_scheduled():
    import schedule
    from update import run_update
    from train_model import train_model

    schedule.every(1).week.do(
        safe_run_job, train_model
    )
    schedule.every(1).week.do(
        safe_run_job, run_update, days_to_fetch=int(os.environ.get("WEEKLY_PULL_PAST_DAYS"))
    )
    schedule.every(1).day.do(
        safe_run_job, run_update, days_to_fetch=int(os.environ.get("DAILY_PULL_PAST_DAYS"))
    )
    schedule.every(1).hour.do(
        safe_run_job, run_update, days_to_fetch=int(os.environ.get("HOURLY_PULL_PAST_DAYS"))
    )

    print("Entering scheduled update loop.")
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            print(f"[SCHEDULE LOOP ERROR] Unexpected error in scheduled loop: {e}", file=sys.stderr)
        time.sleep(10)


if __name__ == "__main__":
    run_scheduled()

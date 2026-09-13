import datetime
import os
import socket
import sys
import time

# Set a process-wide default socket timeout to prevent indefinite socket hangs
socket.setdefaulttimeout(120)

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
    job_name = getattr(job_func, '__name__', str(job_func))
    kwargs_str = f" with {kwargs}" if kwargs else ""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] [SCHEDULE] Starting job '{job_name}'{kwargs_str}...", flush=True)
    try:
        job_func(*args, **kwargs)
        finish_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{finish_str}] [SCHEDULE] Finished job '{job_name}'.", flush=True)
    except Exception as e:
        print(
            f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SCHEDULE ERROR] Job '{job_name}' failed with error: {e}",
            file=sys.stderr,
            flush=True
        )


def log_heartbeat(schedule):
    """Logs the scheduler status and next upcoming jobs."""
    jobs = schedule.get_jobs()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not jobs:
        print(f"[{now_str}] [HEARTBEAT] Scheduler active (no scheduled jobs registered).", flush=True)
        return
    upcoming = sorted(jobs, key=lambda j: j.next_run if j.next_run else datetime.datetime.max)
    next_job = upcoming[0]
    next_time_str = next_job.next_run.strftime("%Y-%m-%d %H:%M:%S") if next_job.next_run else "Unknown"
    job_desc = getattr(next_job.job_func, '__name__', str(next_job.job_func))
    print(f"[{now_str}] [HEARTBEAT] Scheduler active. Next upcoming job: '{job_desc}' scheduled at {next_time_str}.", flush=True)


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

    print("Entering scheduled update loop.", flush=True)
    log_heartbeat(schedule)

    HEARTBEAT_INTERVAL_SECONDS = 6 * 3600  # Every 6 hours
    last_heartbeat = time.time()

    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            print(f"[SCHEDULE LOOP ERROR] Unexpected error in scheduled loop: {e}", file=sys.stderr, flush=True)
            
        current_time = time.time()
        if current_time - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            log_heartbeat(schedule)
            last_heartbeat = current_time

        time.sleep(10)


if __name__ == "__main__":
    run_scheduled()

import functools
import json
import random
import socket
import sys
import time

# Set a process-wide default socket timeout to prevent any indefinite socket hangs
# across all libraries (requests, gspread, urllib, etc.)
socket.setdefaulttimeout(120)

try:
    import requests
except ImportError:
    requests = None

try:
    import gspread
except ImportError:
    gspread = None


def is_transient_error(exc: Exception) -> bool:
    """Check if an exception represents a transient network or API error worth retrying."""
    # 1. Check HTTP response status code if available (e.g. requests.HTTPError or any response object)
    status = getattr(getattr(exc, 'response', None), 'status_code', None)
    if status is not None:
        if status in (429, 500, 502, 503, 504, 520, 521, 522, 523, 524):
            return True
        if 400 <= status < 500:
            return False

    # 2. Requests network errors
    if requests is not None:
        if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return True
        if isinstance(exc, requests.exceptions.RequestException):
            return True

    # 3. JSON Decode Errors (often caused by upstream 5xx HTML error pages or dropped connections)
    if isinstance(exc, json.decoder.JSONDecodeError):
        return True

    # 4. gspread / Google API errors
    if gspread is not None:
        if isinstance(exc, gspread.exceptions.APIError):
            code = None
            if hasattr(exc, 'args') and len(exc.args) > 0 and isinstance(exc.args[0], dict):
                code = exc.args[0].get('code')
            elif hasattr(exc, 'response') and hasattr(exc.response, 'status_code'):
                code = exc.response.status_code
                
            if code in (429, 500, 502, 503, 504, 520, 521, 522, 523, 524):
                return True
                
            msg = str(exc).lower()
            if any(term in msg for term in ["500", "502", "503", "504", "524", "unavailable", "rate limit", "quota", "timeout", "temporarily", "backend error"]):
                return True

        if isinstance(exc, gspread.exceptions.GSpreadException):
            msg = str(exc).lower()
            if any(term in msg for term in ["500", "502", "503", "504", "524", "unavailable", "rate limit", "quota", "timeout", "temporarily", "backend error"]):
                return True

    # 5. Standard library network & connection errors
    if isinstance(exc, (ConnectionResetError, ConnectionRefusedError, TimeoutError, OSError, socket.timeout)):
        return True

    # 6. Fallback string check on error message
    msg = str(exc).lower()
    if any(term in msg for term in [
        "500", "502", "503", "504", "520", "521", "522", "523", "524",
        "service unavailable", "rate limit", "too many requests", "resource_exhausted",
        "timed out", "timeout"
    ]):
        return True

    return False


def retry_api(max_retries=5, initial_delay=2.0, backoff_factor=2.0, jitter=True):
    """
    Decorator for retrying transient API/network calls with exponential backoff.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if is_transient_error(e) and attempt < max_retries:
                        sleep_time = delay * (1 + (random.random() * 0.2 if jitter else 0))
                        print(
                            f"[RETRY WARNING] Transient API error on attempt {attempt}/{max_retries}: {e}. Retrying in {sleep_time:.1f}s...",
                            file=sys.stderr,
                            flush=True
                        )
                        time.sleep(sleep_time)
                        delay *= backoff_factor
                    else:
                        raise
        return wrapper
    return decorator


def retry_call(func, *args, max_retries=5, initial_delay=2.0, backoff_factor=2.0, jitter=True, **kwargs):
    """
    Helper function to wrap a direct function call with retry logic.
    Example: sheet = retry_call(gs.open_by_key, os.environ.get("SHEET_ID"))
    """
    wrapped = retry_api(max_retries=max_retries, initial_delay=initial_delay, backoff_factor=backoff_factor, jitter=jitter)(func)
    return wrapped(*args, **kwargs)

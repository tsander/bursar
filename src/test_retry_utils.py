import unittest
from unittest.mock import MagicMock
import json
import socket
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from retry_utils import is_transient_error, retry_api, retry_call
from run_scheduled import safe_run_job


class DummyAPIError(Exception):
    pass


class DummyHTTPError(Exception):
    def __init__(self, status_code):
        self.response = MagicMock(status_code=status_code)
        super().__init__(f"HTTP Error {status_code}")


class TestRetryUtils(unittest.TestCase):

    def test_is_transient_error_custom_dict(self):
        err_503 = DummyAPIError({'code': 503, 'message': 'The service is currently unavailable.', 'status': 'UNAVAILABLE'})
        self.assertTrue(is_transient_error(err_503))

        err_404 = DummyAPIError({'code': 404, 'message': 'Not found.'})
        self.assertFalse(is_transient_error(err_404))

    def test_is_transient_error_standard_types(self):
        self.assertTrue(is_transient_error(ConnectionResetError("Reset")))
        self.assertTrue(is_transient_error(TimeoutError("Timed out")))
        self.assertTrue(is_transient_error(socket.timeout("Socket timed out")))
        self.assertTrue(is_transient_error(Exception("503 Service Unavailable")))
        self.assertTrue(is_transient_error(Exception("Error 524 A timeout occurred")))
        self.assertFalse(is_transient_error(ValueError("Invalid argument")))

    def test_is_transient_error_json_decode(self):
        try:
            json.loads("<!DOCTYPE html><html>524 Timeout</html>")
        except json.decoder.JSONDecodeError as e:
            self.assertTrue(is_transient_error(e))

    def test_is_transient_error_cloudflare_status_codes(self):
        err_524 = DummyHTTPError(524)
        self.assertTrue(is_transient_error(err_524))

        err_502 = DummyHTTPError(502)
        self.assertTrue(is_transient_error(err_502))

    def test_retry_call_success_on_second_attempt(self):
        mock_func = MagicMock()
        err_503 = DummyAPIError({'code': 503, 'message': 'The service is currently unavailable.'})
        mock_func.side_effect = [err_503, "success"]

        result = retry_call(mock_func, "arg1", max_retries=3, initial_delay=0.01, backoff_factor=1.0, jitter=False)
        self.assertEqual(result, "success")
        self.assertEqual(mock_func.call_count, 2)

    def test_retry_call_exhaustion(self):
        mock_func = MagicMock()
        err_503 = DummyAPIError({'code': 503, 'message': 'The service is currently unavailable.'})
        mock_func.side_effect = err_503

        with self.assertRaises(DummyAPIError):
            retry_call(mock_func, max_retries=3, initial_delay=0.01, backoff_factor=1.0, jitter=False)
        self.assertEqual(mock_func.call_count, 3)

    def test_retry_call_non_transient_no_retry(self):
        mock_func = MagicMock()
        err_400 = ValueError("Bad input")
        mock_func.side_effect = err_400

        with self.assertRaises(ValueError):
            retry_call(mock_func, max_retries=3, initial_delay=0.01, backoff_factor=1.0, jitter=False)
        self.assertEqual(mock_func.call_count, 1)

    def test_safe_run_job_catches_exceptions(self):
        failing_job = MagicMock(side_effect=Exception("API Error 503"))
        failing_job.__name__ = "failing_job"
        
        # Should not raise exception
        try:
            safe_run_job(failing_job, days_to_fetch=1)
        except Exception as e:
            self.fail(f"safe_run_job raised an unexpected exception: {e}")
            
        failing_job.assert_called_once_with(days_to_fetch=1)


if __name__ == '__main__':
    unittest.main()

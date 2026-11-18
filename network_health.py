"""Classify transient DNS errors and bound background-task retry intervals."""
import socket


def temporary_dns_error(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, socket.gaierror) and error.errno == socket.EAI_AGAIN:
            return True
        # libpq wraps resolver failures as OperationalError without a socket cause.
        if "temporary failure in name resolution" in str(error).lower():
            return True
        error = error.__cause__ or error.__context__
    return False


class BackgroundBackoff:
    def __init__(self, base, maximum=300):
        self.base = base
        self.maximum = maximum
        self.failures = 0

    def failed(self):
        self.failures = min(self.failures + 1, 10)
        return min(self.maximum, self.base * 2 ** self.failures)

    def recovered(self):
        failures = self.failures
        self.failures = 0
        return failures

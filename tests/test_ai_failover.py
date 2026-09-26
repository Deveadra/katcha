from katcha.ai.failover import safe_to_fail_over


class _Rejected(Exception):
    def __init__(self, status_code: int, message: str = "rejected") -> None:
        super().__init__(message)
        self.status_code = status_code


def test_explicit_quota_rejection_allows_provider_failover() -> None:
    assert safe_to_fail_over(_Rejected(429, "credit_balance_exhausted")) is True


def test_ambiguous_server_failure_does_not_auto_fail_over() -> None:
    assert safe_to_fail_over(_Rejected(500, "provider may have accepted request")) is False


def test_message_only_credit_exhaustion_allows_provider_failover() -> None:
    assert safe_to_fail_over(RuntimeError("You have no credits remaining")) is True


def test_explicit_high_demand_503_allows_provider_failover() -> None:
    assert (
        safe_to_fail_over(
            _Rejected(
                503,
                "503 UNAVAILABLE: This model is currently experiencing high demand.",
            )
        )
        is True
    )


def test_ambiguous_503_does_not_auto_fail_over() -> None:
    assert safe_to_fail_over(_Rejected(503, "upstream request failed")) is False

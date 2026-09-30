class ApplicationError(Exception):
    def __init__(
        self,
        *,
        code: str,
        status: int,
        message: str,
        retry_after: int | None = None,
        location: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.retry_after = retry_after
        self.location = location
        # A fixed safe code that classifies the error deterministically (B16-R21).
        self.reason = reason

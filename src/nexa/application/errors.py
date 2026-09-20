class ApplicationError(Exception):
    def __init__(
        self,
        *,
        code: str,
        status: int,
        message: str,
        retry_after: int | None = None,
        location: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.retry_after = retry_after
        self.location = location

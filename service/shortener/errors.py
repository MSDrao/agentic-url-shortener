"""Domain errors. The API layer maps these to HTTP status codes."""


class DomainError(Exception):
    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class InvalidInput(DomainError):
    status_code = 422


class NotFound(DomainError):
    status_code = 404


class Gone(DomainError):
    status_code = 410


class Conflict(DomainError):
    status_code = 409


class CodeSpaceExhausted(DomainError):
    status_code = 503

class UnexpectedResult(Exception):
    pass


class HTTPRequestError(Exception):
    """An HTTP error answer, with what the pipeline needs to act on it:
    the status, the body the provider sent and the Retry-After it asked
    for, if any."""

    def __init__(self, status, reason, body='', retry_after=None):
        self.status = int(status or 0)
        self.reason = reason or ''
        self.body = body or ''
        self.retry_after = retry_after
        message = 'HTTP Error %d: %s' % (self.status, self.reason)
        if self.body:
            message += '\n\n' + self.body
        Exception.__init__(self, message)


class ConversionFailed(Exception):
    pass


class ConversionAbort(Exception):
    pass


class TranslationFailed(Exception):
    pass


class TranslationCanceled(Exception):
    pass


class BadApiKeyFormat(TranslationCanceled):
    pass


class NoAvailableApiKey(TranslationCanceled):
    pass


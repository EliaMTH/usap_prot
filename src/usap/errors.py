class USAPError(Exception):
    """Base exception for USAP SDK errors."""


class USAPAmbiguityError(USAPError):
    """A concept or city-object reference matched more than one record."""
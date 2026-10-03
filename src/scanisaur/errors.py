"""The base class for every error Scanisaur raises on bad input."""


class ScanisaurError(Exception):
    """Scanisaur rejected its input: SQL it can't parse or resolve, or a bad catalog fixture."""

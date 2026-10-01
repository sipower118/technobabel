"""Errors shared by the publishing modules.

Both the Graph API client and the asset store raise `PublishError`, so the CLI
has a single exception to catch and turn into a message for the user.
"""

from __future__ import annotations


class PublishError(RuntimeError):
    """Publishing failed in a way the caller should surface to the user."""

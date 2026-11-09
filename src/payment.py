"""Collect CVV and billing ZIP outside the model.

The values go straight from the customer's terminal to the charge call; they
are never returned by a tool, logged or placed in model context.
"""

from __future__ import annotations

import getpass
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

CVV_RE = re.compile(r"^\d{3,4}$")
ZIP_RE = re.compile(r"^\d{5}$")


@dataclass(frozen=True)
class PaymentDetails:
    cvv: str = field(repr=False)
    billing_zip: str = field(repr=False)


class PaymentCollector(Protocol):
    def collect(
        self,
        amount: float,
        currency: str,
        card_type: str,
        last_four: str,
        description: str,
    ) -> PaymentDetails | None: ...


class CliPaymentCollector:
    """Prompts in the terminal; masked when stdin is a terminal."""

    def __init__(
        self,
        read_secret: Callable[[str], str] | None = None,
        print_fn: Callable[[str], None] = print,
    ):
        self._read_secret = read_secret
        self._print = print_fn
        self._warned_unmasked = False

    def _read(self, prompt: str) -> str:
        if self._read_secret is not None:
            return self._read_secret(prompt)
        if sys.stdin.isatty():
            return getpass.getpass(prompt)
        if not self._warned_unmasked:
            self._print("(input is not masked: stdin is not a terminal)")
            self._warned_unmasked = True
        return input(prompt)

    def _read_field(self, prompt: str, pattern: re.Pattern[str]) -> str | None:
        """Read one field; one re-prompt if invalid; empty input declines."""
        for attempt in range(2):
            value = self._read(prompt).strip()
            if not value:
                return None
            if pattern.match(value):
                return value
            if attempt == 0:
                self._print("That doesn't look right. Please try again.")
        return None

    def collect(
        self,
        amount: float,
        currency: str,
        card_type: str,
        last_four: str,
        description: str,
    ) -> PaymentDetails | None:
        self._print(description)
        # No card details while the email is unconfirmed: "the card on file".
        card = (
            f"your {card_type} ending {last_four}"
            if card_type and last_four
            else "the card on file"
        )
        self._print(
            f"Authorize a charge of {currency} {amount:.2f} to {card}. "
            "Press Enter with no input to cancel."
        )
        cvv = self._read_field("CVV: ", CVV_RE)
        if cvv is None:
            return None
        billing_zip = self._read_field("Billing ZIP: ", ZIP_RE)
        if billing_zip is None:
            return None
        return PaymentDetails(cvv=cvv, billing_zip=billing_zip)


class ScriptedPaymentCollector:
    """Returns pre-set responses in order (None once exhausted); for tests and evals."""

    def __init__(self, responses: list[PaymentDetails | None]):
        self._responses = list(responses)
        self.calls: list[tuple[float, str, str]] = []

    def collect(
        self,
        amount: float,
        currency: str,
        card_type: str,
        last_four: str,
        description: str,
    ) -> PaymentDetails | None:
        self.calls.append((amount, card_type, last_four))
        if not self._responses:
            return None
        return self._responses.pop(0)

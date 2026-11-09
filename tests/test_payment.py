from payment import CliPaymentCollector, PaymentDetails, ScriptedPaymentCollector


def _collector(answers):
    it = iter(answers)
    printed = []
    collector = CliPaymentCollector(
        read_secret=lambda prompt: next(it), print_fn=printed.append
    )
    return collector, printed


def _collect(collector):
    return collector.collect(100.49, "USD", "Visa", "4832", "Extend RES-1 by 2 days")


def test_cli_collects_and_restates_amount():
    collector, printed = _collector(["847", "90210"])
    assert _collect(collector) == PaymentDetails(cvv="847", billing_zip="90210")
    text = "\n".join(printed)
    assert "USD 100.49" in text
    assert "Visa ending 4832" in text


def test_cli_without_card_details_says_card_on_file():
    collector, printed = _collector(["847", "90210"])
    collector.collect(100.49, "USD", None, None, "Extend RES-1 by 2 days")
    text = "\n".join(printed)
    assert "the card on file" in text
    assert "None" not in text


def test_cli_empty_is_decline():
    collector, _ = _collector(["", ""])
    assert _collect(collector) is None


def test_cli_reprompts_once():
    collector, _ = _collector(["12", "847", "90210"])
    assert _collect(collector) == PaymentDetails(cvv="847", billing_zip="90210")
    collector, _ = _collector(["12", "1", "90210"])
    assert _collect(collector) is None


def test_repr_hides_values():
    assert "847" not in repr(PaymentDetails("847", "90210"))


def test_scripted():
    first = PaymentDetails("847", "90210")
    collector = ScriptedPaymentCollector([first, None])
    assert collector.collect(10.0, "USD", "Visa", "4832", "d") == first
    assert collector.collect(10.0, "USD", "Visa", "4832", "d") is None
    assert collector.collect(10.0, "USD", "Visa", "4832", "d") is None

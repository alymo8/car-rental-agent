import random
from collections import Counter
from datetime import datetime, timedelta, timezone

from fakes import FakeRoadwayServer, make_deps, make_reservation

from payment import PaymentDetails, ScriptedPaymentCollector
from tools import confirm_extension_impl, quote_extension_impl, verify_customer_impl

PDT = timezone(timedelta(hours=-7))
NOW = datetime(2027, 6, 14, 12, 0, tzinfo=PDT)
RID = "RWR-29471835"
EMAIL = "sarah.johnson@example.com"


def test_fault_injection_200_confirms(tmp_path):
    rng = random.Random(1234)

    def faults():
        if rng.random() < 0.30:
            return rng.choice(["5xx_before", "timeout_before", "timeout_after"])
        return None

    tally: Counter = Counter()
    for i in range(200):
        # Faults start disabled so verify and quote run cleanly.
        server = FakeRoadwayServer({RID: make_reservation()}, {RID: EMAIL})
        collector = ScriptedPaymentCollector([PaymentDetails("847", "90210")])
        directory = tmp_path / str(i)
        deps = make_deps(server, directory, NOW, collector)
        assert verify_customer_impl(deps, RID, EMAIL)["outcome"] == "verified"
        assert quote_extension_impl(deps, "2027-06-17T14:00")["outcome"] == "quoted"

        server.extend_faults = faults
        result = confirm_extension_impl(deps, "q1")

        assert len(server.commits) <= 1
        keys = {
            c["headers"]["Idempotency-Key"]
            for c in server.calls
            if c["endpoint"] == "extend_payment"
        }
        assert len(keys) == 1
        assert result["outcome"] in {"committed", "escalated"}
        if result["outcome"] == "escalated":
            assert result["reason"] == "charge_status_unknown"
            assert list((directory / "handoffs").glob("*.json"))
        label = result["outcome"]
        if label == "escalated" and server.commits:
            label = "escalated_after_commit"
        tally[label] += 1

    print(f"fault-injection tally: {dict(tally)}")
    assert tally["committed"] >= 1
    assert sum(tally.values()) == 200

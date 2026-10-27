"""Load test (D6).

What is being measured
----------------------
A complete run, submission to final result -- not the submission call. The
submission call returns in a few milliseconds by design, so measuring it would
produce a flattering number that says nothing about whether a claims handler
gets an answer in time. `TriageUser.submit_and_poll` therefore issues the POST,
polls until the run reaches a terminal state, and reports the whole thing as a
single `run_complete` sample. Locust's p50 and p95 for that sample are the
numbers in the report.

The provider is replaced by the `llm-stub` service, reached through the ordinary
OpenAI-compatible adapter. That keeps the measurement about our system -- the
queue, the worker pool, the index, the verifier -- rather than about a free
tier's rate limiter, and it keeps the number reproducible between runs.

Run it:

    docker compose --profile loadtest up -d locust
    # then http://localhost:8089, or headless:
    docker compose --profile loadtest run --rm locust \
      locust -f /app/loadtest/locustfile.py --host http://api:8080 \
             --headless -u 20 -r 2 -t 3m --csv /app/loadtest/reports/u20

Before running, point the worker at the stub:

    LLM_PROVIDER=openai_compat
    LLM_BASE_URL=http://llm-stub:9009/v1
    LLM_MODEL=stub-1
    CACHE_RUNS=false        # see the note below
"""

from __future__ import annotations

import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone

from locust import HttpUser, between, events, task

#: Terminal states. Anything else means keep polling.
TERMINAL = {"completed", "escalated", "failed", "rejected"}

POLL_INTERVAL = float(os.environ.get("LOADTEST_POLL_INTERVAL", "0.5"))
POLL_TIMEOUT = float(os.environ.get("LOADTEST_POLL_TIMEOUT", "180"))

#: Each virtual user picks from these. They differ in the work they cause:
#: the restricted-use ones drive the agent through the modifier search and the
#: verification loop, the ordinary ones take the short path. Mixing them keeps
#: the measured p95 representative of a real claims book rather than of the
#: cheapest possible request.
#:
#: Note on caching (D7): the whole-run cache is keyed on the claim body, so a
#: load test that submitted one identical claim would measure the cache, not
#: the system. Every claim generated below is unique in its registration,
#: estimate and description, and `CACHE_RUNS=false` is set for the headline
#: run. The caching effect is measured separately, by re-running with the cache
#: on -- that comparison is the D7 number.
DESCRIPTIONS = [
    (
        "private_social_domestic",
        "I was driving home when the car in front braked hard. I hit the back of it "
        "at low speed. Front bumper and number plate are damaged.",
    ),
    (
        "commuting_to_work",
        "On the way to the office a two-wheeler clipped my driver side mirror and "
        "scratched the door.",
    ),
    (
        "food_or_parcel_delivery",
        "I was making a food delivery run in my own car when the accident happened. "
        "I braked for a dog and went into the kerb. The bumper and headlight are gone.",
    ),
    (
        "ride_hailing",
        "I had a passenger from a ride booking app in the car when a lorry side-swiped "
        "me at the junction.",
    ),
    (
        "private_social_domestic",
        "A stone from a tipper cracked my windscreen on the highway. Nothing else is "
        "damaged and the car drives fine.",
    ),
]


def build_fnol() -> dict:
    use, description = random.choice(DESCRIPTIONS)
    loss = datetime.now(timezone.utc) - timedelta(hours=random.randint(1, 48))
    # The policy period is derived from the loss date rather than hard-coded.
    # A fixed period works until the day it stops: with literal 2025-04-01 and
    # 2026-03-31 dates in here, every generated claim failed the
    # `loss_within_policy_period` cross-field rule, the load test measured
    # nothing but 422s at 2.4 rps, and the run looked fast because no work was
    # being done.
    return {
        "contract_version": "1.0.0",
        "claim_reference": f"LOAD-{uuid.uuid4().hex[:12].upper()}",
        "policy_number": "IL/PC/2025/889012",
        "insurer_code": "ICICI_LOMBARD",
        "product_code": "PRIVATE_CAR_PACKAGE",
        "policy_start_date": (loss - timedelta(days=200)).date().isoformat(),
        "policy_end_date": (loss + timedelta(days=160)).date().isoformat(),
        "date_of_loss": loss.isoformat(),
        "reported_at": (loss + timedelta(minutes=45)).isoformat(),
        "channel": random.choice(["mobile_app", "call_centre", "web_form"]),
        "reporter_phone": "+919845012345",
        "vehicle": {
            "registration_number": f"KA{random.randint(1, 60):02d}MJ{random.randint(1000, 9999)}",
            "make": "Maruti Suzuki",
            "model": "Swift VXi",
            "year_of_manufacture": random.randint(2016, 2023),
            "insured_declared_value_inr": float(random.randint(400, 900) * 1000),
        },
        "driver": {
            "name": "Load Test",
            "age": random.randint(22, 58),
            "licence_number": "KA0120190004567",
            "licence_valid_on_date_of_loss": True,
            "relationship_to_insured": "self",
            "holds_learner_licence_only": False,
        },
        "incident": {
            "description": description,
            "location": {"city": "Bengaluru", "state_code": "KA", "pincode": "560076"},
            "vehicle_use_at_time_of_loss": use,
            "vehicle_drivable": True,
            "third_party_involved": random.random() < 0.3,
            "estimated_repair_cost_inr": float(random.randint(8, 95) * 1000),
        },
    }


class TriageUser(HttpUser):
    """Submits a claim and waits for the answer, as a caller actually would."""

    wait_time = between(1, 3)

    @task(10)
    def submit_and_poll(self) -> None:
        started = time.perf_counter()
        client_id = f"load-{random.randint(1, 1000)}"

        with self.client.post(
            "/v1/claims",
            json=build_fnol(),
            headers={"X-Client-Id": client_id},
            name="POST /v1/claims",
            catch_response=True,
        ) as response:
            if response.status_code == 429:
                # Rate limiting working as designed is not a failure of the
                # system under test, but it must not be counted as a success
                # either -- it would flatter the latency number.
                response.success()
                _report("run_complete", started, exception=RateLimited())
                return
            if response.status_code not in (200, 202):
                response.failure(f"submit returned {response.status_code}")
                _report("run_complete", started, exception=SubmitFailed(response.status_code))
                return
            body = response.json()
            response.success()

        run_id = body["run_id"]
        deadline = time.perf_counter() + POLL_TIMEOUT
        status = "queued"

        while time.perf_counter() < deadline:
            time.sleep(POLL_INTERVAL)
            with self.client.get(
                f"/v1/runs/{run_id}", name="GET /v1/runs/{id}", catch_response=True
            ) as poll:
                if poll.status_code != 200:
                    poll.failure(f"poll returned {poll.status_code}")
                    continue
                poll.success()
                status = poll.json().get("status", "queued")
            if status in TERMINAL:
                break

        if status in TERMINAL:
            _report("run_complete", started)
            if status == "completed":
                self.client.get(f"/v1/runs/{run_id}/result", name="GET /v1/runs/{id}/result")
        else:
            _report("run_complete", started, exception=RunTimeout(POLL_TIMEOUT))

    @task(1)
    def health(self) -> None:
        self.client.get("/health", name="GET /health")


class RateLimited(Exception):
    def __str__(self) -> str:
        return "rate limited by the service"


class RunTimeout(Exception):
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def __str__(self) -> str:
        return f"run did not reach a terminal state within {self.seconds}s"


class SubmitFailed(Exception):
    def __init__(self, status: int) -> None:
        self.status = status

    def __str__(self) -> str:
        return f"submission returned HTTP {self.status}"


def _report(name: str, started: float, exception: Exception | None = None) -> None:
    events.request.fire(
        request_type="RUN",
        name=name,
        response_time=(time.perf_counter() - started) * 1000,
        response_length=0,
        exception=exception,
        context={},
    )


#: A run above this failure ratio is not a measurement of anything. Reporting a
#: latency percentile over mostly-failed samples is how you end up declaring an
#: objective met by a test that submitted nothing valid.
MAX_FAILURE_RATIO = 0.02


@events.test_stop.add_listener
def _summarise(environment, **_kwargs) -> None:
    """Print the SLO verdict at the end of a headless run."""
    target = float(os.environ.get("SLO_P95_SECONDS", "45"))
    stats = environment.stats.get("run_complete", "RUN")
    if not stats or not stats.num_requests:
        print("\nno complete runs recorded\n")
        return

    p50 = stats.get_response_time_percentile(0.5) / 1000.0
    p95 = stats.get_response_time_percentile(0.95) / 1000.0
    rps = stats.total_rps
    failures = stats.num_failures
    ratio = failures / stats.num_requests

    if ratio > MAX_FAILURE_RATIO:
        verdict = f"INVALID -- {ratio:.0%} of runs failed; fix the test, not the target"
    elif p95 <= target:
        verdict = "MET"
    else:
        verdict = "NOT MET"

    print("\n" + "=" * 70)
    print("  D6 -- complete run, submission to final result")
    print("=" * 70)
    print(f"  runs            {stats.num_requests}   failures {failures} ({ratio:.1%})")
    print(f"  throughput      {rps:.2f} runs/s")
    print(f"  p50             {p50:.2f}s")
    print(f"  p95             {p95:.2f}s")
    print(f"  objective       {target:.2f}s")
    print(f"  VERDICT         {verdict}")
    print("=" * 70 + "\n")

"""Test configuration.

The tests run with no infrastructure at all: the in-memory state backend, the
deterministic hash embedder, a local numpy vector store in a temporary
directory, and the in-process stub provider. That is deliberate -- a test suite
that needs Redis and Chroma up is a test suite people stop running.

What this therefore does *not* test is Redis, Chroma, the HTTP hop to the
provider, or anything about the containers. Those are covered by the
demonstration items and the load test, and the gap is stated in docs/testing.md
rather than papered over.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _configure_environment(index_dir: Path) -> None:
    os.environ.update(
        {
            "LLM_PROVIDER": "stub",
            "STUB_LATENCY_MS": "0",
            "STUB_LATENCY_JITTER_MS": "0",
            "STUB_FAILURE_RATE": "0.0",
            "EMBEDDING_BACKEND": "hash",
            "VECTOR_BACKEND": "local",
            "INDEX_DIR": str(index_dir),
            "CORPUS_MANIFEST": str(REPO_ROOT / "corpus" / "sources.yaml"),
            "PROMPT_DIR": str(REPO_ROOT / "prompts"),
            "PROMPT_SET": "v2",
            "REDIS_URL": "memory://test",
            "EXECUTION_MODE": "inline",
            "TRACING_ENABLED": "false",
            "LOG_FORMAT": "console",
            "LOG_LEVEL": "WARNING",
            "CACHE_RUNS": "false",
            "CACHE_RETRIEVALS": "false",
            "CACHE_EMBEDDINGS": "false",
            "RATE_LIMIT_ENABLED": "false",
            "AGENT_WALL_CLOCK_SECONDS": "60",
        }
    )


@pytest.fixture(scope="session")
def index_dir(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("index")
    _configure_environment(directory)
    return directory


@pytest.fixture(scope="session", autouse=True)
def configured(index_dir: Path):
    """Make the environment live before anything imports the settings cache."""
    from claimtriage.config import reload_settings

    reload_settings()
    yield


@pytest.fixture(scope="session")
def synthetic_corpus(tmp_path_factory) -> Path:
    """A tiny wording with the same shape as the real one.

    The real 46-page PDF is not committed to the repository (see
    corpus/sources.yaml), so the tests cannot depend on it being present. This
    fixture reproduces the *structure* the system relies on -- a grant in one
    section, a general exception that names a restriction by name, and the
    restriction itself in a separate chapter -- which is what every retrieval
    and verification assertion here is actually about.
    """
    directory = tmp_path_factory.mktemp("corpus")
    (directory / "raw").mkdir()
    (directory / "raw" / "test_wording.txt").write_text(
        """PRIVATE CAR PACKAGE POLICY WORDING

SECTION I - LOSS OF OR DAMAGE TO THE VEHICLE INSURED
The Company will indemnify the insured against loss or damage to the vehicle
insured caused by accidental external means, by fire, or by burglary, housebreaking
or theft.

Chapter 2 - Exception, Condition & Deductible
1. GENERAL EXCEPTIONS (Applicable to all Sections of the Policy)
The Company shall not be liable under this Policy in respect of
I. Any accidental loss or damage caused outside the geographical area;
II. Any claim arising out of any contractual liability;
III. Any accidental loss damage and/or liability incurred whilst the vehicle
insured herein is
 a. Being used otherwise than in accordance with the 'Limitations as to Use' or
 b. Being driven by any person other than a Driver as stated in the Driver's Clause.

Chapter 3 - Conditions
Notice shall be given in writing to the Company immediately upon the occurrence
of any accidental loss or damage.

CHAPTER 4 - Limitations as to Use
The Policy covers use of the vehicle for any purpose other than:
Hire or Reward, Carriage of goods (other than samples of personal luggage),
Organised racing, Pace Making, Reliability trails or Speed testing, any purpose
in Connection with Motor Trade.

CHAPTER 5 - Driver's Clause
Any person including the insured: Provided that a person driving holds an
effective driving license at the time of the accident and is not disqualified
from holding or obtaining such a license.

CHAPTER 6 - Claim Procedure
Claim Intimation: the insured shall intimate the claim through the toll-free
number, the website or the mobile application. A licensed surveyor will be
appointed within 24 hours of reporting where the claim requires inspection.
""",
        encoding="utf-8",
    )
    (directory / "sources.yaml").write_text(
        """
insurer:
  name: Test Insurer
  product: Private Car Package Policy

documents:
  - document_id: test_wording
    title: "Test Insurer - Private Car Package Policy - Policy Wording"
    filename: test_wording.txt
    url: ""
    kind: text
    role_hint: policy_wording
    enabled: true
""",
        encoding="utf-8",
    )
    return directory


@pytest.fixture(scope="session")
def built_index(synthetic_corpus: Path, index_dir: Path):
    """Ingest the synthetic corpus once for the whole session."""
    from claimtriage.config import reload_settings
    from claimtriage.retrieval.embeddings import reset_embedder
    from claimtriage.retrieval.index import ClauseIndex, reset_index
    from claimtriage.retrieval.ingest import run_ingest

    os.environ["CORPUS_MANIFEST"] = str(synthetic_corpus / "sources.yaml")
    reload_settings()
    reset_embedder()
    reset_index()
    run_ingest(force=True, index=ClauseIndex())
    reset_index()
    from claimtriage.retrieval.index import get_index

    index = get_index()
    assert index.size > 0, "the synthetic corpus produced no chunks"
    return index


@pytest.fixture
def valid_fnol() -> dict:
    return {
        "contract_version": "1.0.0",
        "claim_reference": "TEST-0000000001",
        "policy_number": "IL/PC/2025/889012",
        "insurer_code": "ICICI_LOMBARD",
        "product_code": "PRIVATE_CAR_PACKAGE",
        "policy_start_date": "2025-04-01",
        "policy_end_date": "2026-03-31",
        "date_of_loss": "2025-11-04T19:40:00+05:30",
        "reported_at": "2025-11-04T21:05:00+05:30",
        "channel": "mobile_app",
        "reporter_phone": "+919845012345",
        "vehicle": {
            "registration_number": "KA01MJ2023",
            "make": "Maruti Suzuki",
            "model": "Swift VXi",
            "year_of_manufacture": 2021,
            "insured_declared_value_inr": 620000.0,
        },
        "driver": {
            "name": "A Policyholder",
            "age": 31,
            "licence_number": "KA0120190004567",
            "licence_valid_on_date_of_loss": True,
            "relationship_to_insured": "self",
            "holds_learner_licence_only": False,
        },
        "incident": {
            "description": (
                "Someone reversed into my parked car in the basement. There is a "
                "dent on the rear door and a scratch along the quarter panel."
            ),
            "location": {"city": "Bengaluru", "state_code": "KA", "pincode": "560076"},
            "vehicle_use_at_time_of_loss": "private_social_domestic",
            "vehicle_drivable": True,
            "third_party_involved": False,
            "estimated_repair_cost_inr": 14500.0,
        },
    }

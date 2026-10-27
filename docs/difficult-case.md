# The section 4.1 case

> *"I was making a food delivery run in my own car when the accident happened."*

This is the case the system is tested against at the demonstration. This file is
what was shown at Evaluation 1: the exact passages in the corpus that the case
depends on, the values it will be tested with, and the reference answer.

## Why it is hard

A search about an accident retrieves the own-damage cover. On that passage alone
the claim is covered. The provision that decides the outcome is somewhere else
entirely, and the request does not resemble it.

The case turns on **three** passages in two different chapters, and a correct
answer has to find all three and show how they interact.

### 1. The grant — Chapter 1, Section I

**`SECTION I. LOSS OF OR DAMAGE TO THE VEHICLE INSURED`** (page 1)

> The Company will indemnify the insured against loss or damage to the vehicle
> insured … by accidental external means …

This is what a search for *"accident, bumper and bonnet damaged"* returns. It is
the passage a retrieve-then-answer pipeline stops at.

### 2. The restriction — Chapter 4

**`CHAPTER 4 - Limitations as to Use`** (page 19, role `restriction`)

> The Policy covers use of the vehicle for any purpose other than:
> Hire or Reward, **Carriage of goods (other than samples of personal luggage)**,
> Organised racing, Pace Making, Reliability trails or Speed testing, any purpose
> in Connection with Motor Trade.

Note what this passage does *not* contain: the words accident, damage, bumper,
claim, cover, delivery, or food. There is no phrasing of the customer's sentence
whose embedding lands near it. It shares exactly one content word with the
request — *car*, and it does not even use that word.

### 3. The bridge — Chapter 2, General Exceptions III(a)

**`Chapter 2 - Exception, Condition & Deductible > 1. GENERAL EXCEPTIONS > III > a`**
(page 3, role `exclusion`)

> The Company shall not be liable under this Policy in respect of … III. Any
> accidental loss damage and/or liability caused sustained or incurred whilst the
> vehicle insured herein is **a. Being used otherwise than in accordance with the
> 'Limitations as to Use'** …

This is the passage that makes the restriction bite on the grant. Without it, a
reader could note the Limitations as to Use and still not know what follows from
breaching them — the restriction itself states no consequence.

It is also the key to finding the restriction at all: **this clause names it.**

## How the system finds all three

Three mechanisms, each built for this.

**At index time — cross-reference extraction.** The chunker scans each chunk for
the names of clauses the wording refers to by name (`NAMED_CLAUSES` in
`claimtriage/retrieval/chunker.py`) and records them on the chunk. The general
exception above therefore carries:

```json
"references": ["Limitations as to Use", "Driver's Clause"]
```

Verified by `tests/test_retrieval.py::test_general_exception_records_its_cross_reference`.

**At index time — role tagging.** Every chunk is tagged `grant`, `restriction`,
`exclusion`, `condition`, `procedure` or `definition`, inferred from its heading
path and its own wording. That makes a role-filtered search possible: *"what,
anywhere in this document, restricts or excludes?"* — a question that has no
answer in the customer's vocabulary.

**At run time — a planning step that decides to look.** After the grant is
retrieved, the Coverage Analyst is asked, by `prompts/v2/query_formulation.md`,
whether any of exactly four things could displace it — Limitations as to Use,
Driver's Clause, General Exceptions, Conditions — and to report for each whether
it has checked. It must write its queries in the language of the policy, and the
prompt says so outright:

> Write your queries in the language of the *policy*, not the language of the
> customer. "Food delivery" will not match; "hire or reward", "carriage of goods"
> and "limitations as to use" will.

The graph then resolves what it names with `find_named_clause`, which looks the
clause up **by name** rather than by similarity. That tool exists for this case.

The prompt-set comparison in [evaluation.md](evaluation.md) isolates this: `v1`
asks only for "any other relevant clauses", and the model answers by restating
the clause it already has. Change C1 in `prompts/registry.yaml` is exactly this
difference.

## The test values

These are the values the reference case is submitted with. They are
`S01_food_delivery_reference_case` in `eval/dataset.yaml`, and the same values
are pre-loaded in the web client and used by `scripts/demo.sh`.

| field | value | why this value |
|---|---|---|
| `incident.description` | *"I was making a food delivery run in my own car when the accident happened. A two-wheeler cut across at the junction, I braked hard and hit the kerb. The front bumper and bonnet are damaged and the left headlight is broken."* | The brief's sentence, plus enough ordinary accident detail that a naive search reliably returns the own-damage grant. The whole difficulty is that this text pulls hard toward the grant. |
| `incident.vehicle_use_at_time_of_loss` | `food_or_parcel_delivery` | The structured counterpart. The validator flags it as a warning, not an error — see below. |
| `incident.estimated_repair_cost_inr` | `48000` | Under the INR 50,000 fast-track ceiling and under 75% of the IDV, so **nothing but the coverage position can push this claim out of fast-track.** If the estimate were high, the routing would be right by accident. |
| `incident.vehicle_drivable` | `true` | Same reason: removes the surveyor trigger. |
| `incident.third_party_involved` | `false` | Same reason: removes the third-party trigger. |
| `vehicle.insured_declared_value_inr` | `620000` | Makes the 48,000 estimate 7.7% of IDV — nowhere near a total loss. |
| `driver.licence_valid_on_date_of_loss` | `true` | Removes the driver's-clause trigger, so the Limitations as to Use is the *only* thing that can change the answer. |
| `date_of_loss` / `reported_at` | 2025-11-04, 85 minutes apart | Inside the policy period, promptly notified — removes the late-notification condition. |

The deliberate design of those values is the point: every other route to
"investigate this" has been closed off. If the system routes this claim to
investigation, it is because it found and applied the limitations clause.

## The reference answer

A run is **correct** when all of the following hold. These are enforced
automatically by the evaluation harness, not checked by eye.

| | requirement |
|---|---|
| position | `likely_not_covered`, `partially_covered` or `undetermined` — **not** `likely_covered` |
| queue | `investigation` (or `human_escalation` if it escalated honestly) |
| citations | at least one, and **every** returned citation resolves character-for-character to the clause it names |
| citation content | at least one citation touches *limitations as to use* or *general exception* |
| prose | the determination mentions at least one of: *hire or reward*, *limitations as to use*, *permitted*, *purpose* |
| verification | if the run completed, the verdict is `verified` |

`undetermined` is accepted deliberately. If the system finds both passages and
says *"this turns on whether the delivery was for reward, which the FNOL does not
establish — a handler must find out"*, that is a better answer than a confident
decline. What is **not** accepted is `likely_covered`, which means it never found
the restriction, or a confident decline with no citation, which means it guessed.

### What a good answer says

The open question matters as much as the position. The limitations clause
excludes *carriage of goods* and *hire or reward*. Whether a food delivery run is
either depends on facts the FNOL does not contain: was the driver paid, was the
food a consignment being carried for reward, or was it a favour for a neighbour.
A system that declares the claim declined has over-reached; a system that notes
the clause, states that it is engaged on the face of it, and tells the handler
what to establish has done the job.

## The control case

`S02_commuting_is_permitted` exists because of a specific way to pass S01 for the
wrong reason. Driving to work is also a *purpose*. A system that has learned
"description mentions a purpose → restricted use → decline" scores well on S01
and is useless.

| | S01 | S02 |
|---|---|---|
| declared use | `food_or_parcel_delivery` | `commuting_to_work` |
| expected position | not `likely_covered` | `likely_covered` or `partially_covered` |
| expected queue | `investigation` | `fast_track` or `surveyor_assignment` |

Commuting is not among the restricted purposes. A correct system reads the
clause and notices that. The restricted-use family — ride-hailing (S10),
carriage of goods (S11), speed testing (S12), driving tuition (S13) — tests the
same mechanism from the other side.

## Checking the corpus yourself

The passages above can be pulled straight out of the index:

```bash
# the restriction, by name
docker compose run --rm tools search "hire or reward carriage of goods" --strategy lexical

# only restrictions and exclusions, anywhere in the wording
docker compose run --rm tools search \
  "use of the vehicle for food or parcel delivery permitted purposes" --role restriction

# the bridge clause, and what it points at
docker compose run --rm tools search "otherwise than in accordance with the limitations as to use"

# one clause with its neighbours
docker compose run --rm tools clause \
  il_private_car_package_wording::chapter-4-limitations-as-to-use::0151 --neighbours 2
```

And the reference case can be run on its own:

```bash
docker compose run --rm tools eval --only S01_food_delivery_reference_case
```

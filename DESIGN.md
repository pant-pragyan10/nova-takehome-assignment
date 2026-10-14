# Design

## Fact model

Every email and attachment is stored once under `--state-dir` with its text pages, sender, UTC received date and kind. Each document then gets one **facts** record (`understand.py`) carrying `method` (rules or model) and any validation issues.
Facts keep their source: every table row holds the printed line and page (`USE2-INE2 100-299.5 kg 6.01 6.16`, p.8), every email fact holds the sentence it came from and whether it was quoted text, every clause keeps its number and page.
Evidence in a report is therefore copied, never composed. `report` drops any quote it cannot find on the cited page. The model only *reads*: contract clauses, invoices and emails the rules fail on.
Its answers are accepted only if they can be checked: invoice lines must add to the printed totals, email quotes must appear verbatim, numbers in a clause answer must appear in the clause. All money, date and policy logic is plain Python (`pricing.py`, `engine.py`), so `report` is deterministic and makes no model calls.

## Conflicts

Rates for (contract, lane, pickup date) are resolved from the MSA, signed amendments (latest effective date wins), an attached Annex A revision, and rate notices from a Schedule 1 `rates` person (or a notice a person accepted in feedback).
If an amendment and a notice are both in force, the contract's precedence term decides: `amendment_wins` / `notice_wins`, else "unranked" which gives a **conflict**. I price the invoice under both and report `[max(0, billed-higher), billed-lower]`, action `escalate`, outside the totals.
A source without authority (sender not on Schedule 1, quoted text, unsigned draft) never conflicts; the stronger source simply wins. If the deciding rate sits in a document I do not hold (an Annex A revision named in an amendment) the key is **cannot_determine**, with the document's exact name and the Schedule 1 `rates` contact as holder.

## Confidence

Start at 0.95 for a finding priced entirely from documents of level 1-2 with pickup from tracking. Subtract 0.08 when the pickup came from the AWB, 0.15 when it came from the invoice's own date, 0.07 when a source was a scan read by OCR. Conflicts 0.9, cannot_determine 0.9, cashback 0.9, late-fee risk 0.92, expiry 0.95.
The practice Brier score is 0.0042. Weakness: no wrong item exists in the practice set, so I could not measure AUROC; the numbers are judgement, not fitted.

## People in the loop

Supervised: internal actions run; outbound remedies are `draft:*`; a finding totalling $5,000.00 or more on one invoice (or one cashback key) is `human_review`; conflicts `escalate`. A hard assertion forbids `execute:*` in supervised mode.
Autonomous: `execute:*` only when all five checks pass: no missing document and pickup from tracking or a carrier email; no conflict on the key or on the same contract, lane and rate period; under $5,000; an undoable action (dispute, claim, document request); every term from level 1-2 and a Schedule 1 recipient.
Otherwise `human_review`. Accepting terms, confirming renewals and paying are never produced. Text aimed at "the AI" is recorded as an `instruction` fact and ignored; a test (`oc_injection_ignored`) covers it.

## Feedback

`feedback` stores each item with its file, batch, `decided_at` and decider. A report uses an item only from `decided_at` on. Standing facts apply only when every `applies_to` filter matches inside `valid_from..valid_to`
(`sender_authority`: person + category + contract; `rate_accepted`: contract + lane + dates; `precedence`: contract + lane). One-off decisions (`close`, `reject`, `one_off_approval`, `override_amount`) change only their own key.
Anything changed this way carries `learned_at`.

## New documents

State is cumulative and `report --as-of D` rebuilds from documents received by D. Each report is saved; the next one is compared with the latest earlier report: a key whose verdict or amount changed lists the old id in `supersedes`; a key that appears for an invoice already reported on, because a later document reached back, gets `retro: true`. A key whose cause is gone simply disappears.
New layouts are handled by arithmetic, not templates: each invoice line is decoded by `rate x qty = amount` or `pct x base = amount`, so a new column order still parses, and a line that does not reconcile goes to the model.

## Cost

Measured on both mailboxes with rules only: 249 documents, 0 model calls, about 15 s for ingest plus four reports. Estimate with the model path: a fallback call is about 3k tokens in and 1k out, roughly $0.025 at Sonnet prices.
If 5% of documents need it, that is about $1.25 per 1,000 documents, worst case (every document) about $25; responses are cached on disk, so re-running costs nothing. OCR of a scanned invoice takes a few seconds on CPU. These model figures are estimates: I could not run a live model.

## What I'd do next

Run everything once against the real model and read the failures. Test margin step-down and auto-renewal on real wordings (I had none). Read the peak-season surcharge months, check chargeable weight against the AWB, and treat quoted rate notices as `cannot_determine`.
Least sure: how the grader names a missing quoted email, and whether `retro` should also mark keys that reappear after feedback.

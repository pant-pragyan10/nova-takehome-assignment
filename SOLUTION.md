# My solution

Read this first. The brief stays in README.md, GLOSSARY.md and BRIEF.pdf.

## How to run it

```
make setup                      # .venv + pinned requirements (includes the local OCR for scanned invoices)
make grade                      # practice mailbox -> report_card.txt / report_card.json
make sheet                      # exam mailbox     -> out/exam_report.json, answer_sheet.json
make eval                       # my 18 cases + 2 samples -> evals/results.json
make validate
```

The grader calls `python harness/harness.py ingest|report|feedback`. Nothing else is needed.

**Model access.** `ingest` calls the Anthropic SDK (`harness/llm.py`) and reads `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` from the environment.
The model is asked to *read* a document only when my deterministic readers cannot (see DESIGN.md). `report` never calls a model.
With no key, or with `NOVA_NO_LLM=1`, the harness runs the deterministic readers only. That is how `report_card.*`,
`answer_sheet.json` and `evals/results.json` were produced (the sandbox I built this in had no model key), and both mailboxes
read fully this way: every one of the 163 practice and 86 exam documents was read by rules, with no validation issue.
For local development without an Anthropic key, `tools/groq_proxy.py` translates Anthropic Messages calls to Groq
(`export GROQ_API_KEY=...; python tools/groq_proxy.py &; export ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ANTHROPIC_API_KEY=x`).
It is a dev tool only; the key is read from the environment and is not in the repo.

## What works

* Practice mailbox: **99.9 / 100** (`report_card.txt`): F1 1.00 (22 hits, 0 false, 0 missed), amounts $14,140.09 of $14,140.09 with $0.00 wrongly disputed, adaptation 11/11,
  learning 3/3, conflict and abstention 2/2, clean controls 16/16, no hard gate tripped. The only lost point is calibration: with no wrong item, AUROC is not measurable.
* Exam mailbox: `answer_sheet.json` is written for 2031-02-28 (1 conflict, 1 cannot_determine, expiry, rebate, one late-fee risk). I cannot see the key, so I checked it by hand against the documents.
* `make eval`: 20 of 20 (18 mine, built in `evals/build_cases.py` from the shipped contracts plus invoices, trackings and ledgers I generated; the expected numbers are worked out there with independent Decimal arithmetic).
  The suite does catch regressions: I moved the period boundary from `<=` to `<` in `pricing.py` and `oc_period_boundary_clean` failed (`expected no finding, got finding 28.39`); I restored it.
* All six classes plus conflicts, abstention, `supersedes`/`retro`, `learned_at` for the feedback kinds in GLOSSARY, both modes, the $5,000 rule and the five-check test for `execute:*`.
  `report` asserts that supervised mode never emits an `execute:*` action.

## What doesn't

* **The model path has never run against a real model.** My sandbox could not reach Anthropic or Groq. The guards around it are unit-tested with faked replies
  (a reply is only used if invoice lines reproduce the printed totals, every quote is found verbatim in the email, every number in a clause answer is in the clause), but a live run may surface prompt problems. Please run `make grade` once with a key.
* **Handling-margin step-down is untested.** All shipped contracts say "Not applicable"; I wrote the rule from GLOSSARY and let the model read the clause wording, but I had no document to test it on.
  Auto-renewing contracts (notice-deadline expiry) have the same status: coded and sanity-tested on a synthetic term, never seen on a real contract.
* Not checked: peak-season surcharge (the Annex D basis text is cut by a page break in the shipped contracts), chargeable weight against the AWB (I price the weight as printed, as GLOSSARY says), accessorial rate changes by amendment.
* A quoted waiver gets `missing_doc.name` = the quoted email's subject (or "Original waiver email"); GLOSSARY does not say what name the grader expects.
* Rate notices that are only quoted are ignored rather than raised as `cannot_determine`.

## The eval case that caught a bug

`CDU-3151580` first showed a late-fee refund of $45,472.75 in `out/exam_report.json`, which is absurd for a $340 invoice. Cause: the AP-register parser looked for a
"currency + amount" cell in the *first* column too, so the invoice number `CDU-3051334` was read as currency `CDU`, amount `-3051334`. It never showed on the practice mailbox
(its numbers start `TX-`) and broke on the exam's `CDU-` numbers. Fix: `tables.parse_register` searches only the cells after the invoice number.

I then wrote `lf_threeletter_ids_ok` and `lf_threeletter_ids_excess`, which reproduce it (a late fee on `ABC-2000001`, paid 10 days late). With the old line restored both fail
(`expected no finding, got finding 23811.91`); with the fix they pass. The same session fixed the clause splitter, which cut clause 5.2 at the wrapped line "Annex D, once per shipment...".
`oc_period_boundary_clean` is the case I used to check the suite can fail at all: moving the period boundary from `<=` to `<` in `pricing.py` made it fail (`got finding 28.39`).

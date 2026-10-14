# AI use

## Tools

* **Claude Code** (Anthropic's coding agent, running Claude Sonnet 5.5; a few early turns ran on Sonnet 4.6) in a cloud sandbox, driven by me through chat. It wrote nearly all the code, the eval builder and the drafts of these three documents.
* **Groq free tier** was my only model key, so Claude Code wrote `tools/groq_proxy.py` to let the Anthropic-SDK code run against Groq for development. The submitted harness calls the Anthropic SDK.
* No other AI tool.

## How you worked with them

I gave Claude Code the repository and asked for a step-by-step plan, then told it to stop planning and build each section in turn (ingestion, extraction, rules engine, feedback, evals, write-ups).
Claude Code read the README, GLOSSARY and the worked examples, wrote each module, and checked it against the practice answer files and the exam mailbox. I did not write the code myself.
What I (and the session) checked: the practice report card (99.9/100), the exam answer sheet by reading the report against the documents, and the eval suite, including a deliberate break to see a case fail.
Where it went wrong, and was fixed: the register parser read `CDU-3051334` as a currency and amount; the clause splitter cut clauses at lines starting "Annex D,"; scanned invoices and some layouts were unclassified at first;
the sandbox could not reach any model API, so the model path was only tested with faked replies and has not run live. I have not independently verified the exam answer sheet beyond reading it against the documents.

## Written without AI

The decision to build the deterministic-first design was Claude Code's suggestion, which I accepted. I wrote none of the code by hand. The task choice, the instruction to build section by section, and the final review are mine.

## Prompt log

My own messages, verbatim, are in [`ai_log/user_prompts.md`](ai_log/user_prompts.md). The session was otherwise Claude Code working autonomously with tools (file edits, shell runs, the repo's graders); the assistant side of the transcript is the session linked in each commit message:
https://claude.ai/code/session_01KgBA8Vcx7jw646yU6t8Xhs

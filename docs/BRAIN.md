# Building the brain

Handoff note for whoever picks this up next. Read `CLAUDE.md` first for
architecture; this file covers only the work of turning the crawled corpus into
something that answers like an expert, and what is known vs. assumed about it.

## Where things stand

The gathering phase is finished. Both live sources ran to completion:

- **Oracle Help Center** — 24,343 pages ingested, queue exhausted. The knowledge
  base grew 16,016 chunks in the final ~18 hours (44,353 → 60,369).
- **SuiteTalk SOAP schemas** — 183 record types, 6,140 fields, 638 sublists. The
  catalog is 100% `soap_schema`.
- **Records Browser** — retired. NetSuite took those pages offline (every
  version redirects to `page_not_found`). Its parser was never exercised against
  a real page and never will be. Do not spend a session repairing it.

Two blemishes in the docs run, both examined and left alone: 295 pages skipped
as too short (1.2%, nav and redirect stubs) and 23 genuine 404s from stale links
in Oracle's own navigation.

So the raw material exists. What does not yet exist is evidence that the agent
reasons like an expert over it.

## The measurement problem — fix this first

`eval/baseline_hard_pre_distill.json` and `eval/baseline_standard_pre_distill.json`
look encouraging: 29/30 on the hard set, 32/35 on the standard set. **Those are
keyword scores only.** Every result row in both files has `judge_pass: null` and
an empty `judge_reason` — the Claude judge never ran, almost certainly because
`ANTHROPIC_API_KEY` was not set in the process running the eval.

Keyword scoring asks whether a substring appears anywhere in the answer. On
judgment questions that is nearly meaningless: an answer can contain the words
"revenue arrangement" and still be wrong about what happens. Treat 29/30 as
unmeasured, not as a high score.

First task for the next session:

```bash
export ANTHROPIC_API_KEY=sk-...
python eval/run_eval.py --api https://web-production-5ff2c9.up.railway.app \
  --file eval/netsuite_hard.jsonl
```

Confirm the run prints a `Judge score:` line. If it does not, the judge is still
disabled and the numbers are still keyword artifacts. Re-record both baselines
once the judge genuinely runs — the current ones cannot support a before/after
comparison of distillation, which is what they were captured for.

## The bar: the founder's questions

Five questions were supplied by the product owner, who is the NetSuite expert on
this project. They are in `eval/netsuite_hard.jsonl`, tagged `"source":
"founder"`:

- `suitebilling-item-blockers` — item record decisions that make an item unusable
  on a SuiteBilling subscription
- `direct-posting-vs-arm` — why direct revenue posting items and rev-rec items
  can't share a sales order
- `csv-run-server-scripts` — what "Run Server SuiteScript and Workflows" on a CSV
  import actually does
- `line-level-shipping` — whether shipping addresses can vary by line
- `adjustment-only-vs-multibook` — adjustment-only book vs. full multi-book

These are the shape of question that matters: consequences and constraints, not
field IDs. The existing standard set (`netsuite_qa.jsonl`) is almost entirely
lookup — internal IDs, sublist names — which the catalog answers without any
understanding at all. Weight the hard set accordingly when judging progress.

**Their `expect_any` hints were written by an agent, not by the expert, and are
the least trustworthy thing in this file.** They are hints for the judge, not
ground truth. The hints on `suitebilling-item-blockers` in particular are a
guess at which item-type constraints matter. Before trusting a pass or a fail on
these five, get the expected answers confirmed by the product owner and correct
the hints. A judge grading against wrong hints produces confident nonsense in
both directions.

## Distillation

`knowledge_base/distill.py` is built and wired (`POST /knowledge/crawl/start`
with `target: distill`, `GET /knowledge/distill/stats`). It works in two
resumable phases: propose topics, then write one grounded guide per topic, stored
as `doc_type='guide'`, `source='distilled_guide'`. Retrieval genuinely does
prefer guides — `index.py` subtracts `_GUIDE_BOOST` from the distance for
`doc_type='guide'` — so a written guide outranks the fragments it came from.

### Two kinds of guide, and why

Topics of `kind='topic'` are proposed from real page titles within a module.
That yields guides shaped like Oracle's documentation: one feature at a time,
"Setting up multi-book accounting".

That shape cannot answer most of the founder's questions. "Why can't direct
posting items and rev-rec items share a sales order" is not a page title in
Oracle's docs and never will be — it is the knowledge a consultant assembles by
reading across several features. Feature-shaped topics proposed from feature-
shaped titles will not produce it.

So `kind='interaction'` proposes questions instead of topics: consequences,
constraints, what one setting forbids elsewhere, what a feature quietly changes
once enabled. Each carries 2-3 retrieval queries stored in `kb_topics.queries`,
one per feature involved, because a single semantic search on the question tends
to return passages about whichever feature it names first and starve the other
side. `_retrieve` runs each query, merges, dedupes on the leading 200 characters
keeping the higher-scoring copy, and excludes previously written guides so
distillation never feeds on itself.

`GET /knowledge/distill/stats` reports `by_kind` so the two are visible
separately.

**This pass has been unit-tested against stubs, not run against the live
corpus.** Multi-query retrieval, dedup, guide exclusion and malformed-proposal
handling are verified. Whether the interaction proposals are any *good* — whether
Claude proposes real consultant questions or plausible-sounding filler when shown
120 page titles — is unknown and is the first thing to look at. Read twenty of
the proposed questions before letting it write guides for all of them:

```
POST /knowledge/crawl/start {"target": "distill", "max_pages": 5}
```

then read the questions themselves:

```
GET /knowledge/distill/topics?kind=interaction&limit=25
```

If the questions are weak, fix `_INTERACTION_PROMPT` before spending a full run.
Judge them against the founder's five: a good proposal names two specific
NetSuite features and asks what one does to the other. A bad one is a feature
description wearing a question mark ("How do I configure revenue recognition?").

Whether it has actually been run against the live corpus is **not established
here** — this session had no HTTP access to check. Determine it before doing
anything else with it:

```
GET /knowledge/distill/stats
```

If the guide count is zero, the distillation has not run and the pre-distillation
baselines are still the current state of the system. If it is non-zero, compare
against the baselines — but only after the judge is working, per above.

## Suggested order of work

1. Get the judge running; re-record both baselines with real judge scores.
2. Confirm the founder questions' expected answers with the product owner; fix
   the hints.
3. Establish whether distillation has run. If not, run it and measure the
   before/after on the hard set.
4. Read the actual failures, not the score. A wrong answer on
   `direct-posting-vs-arm` tells you something specific about what the corpus
   failed to convey; the aggregate number does not.
5. Grow the corpus only where a failure traces to missing material rather than
   missing synthesis. The crawl is complete, not stalled — more pages requires
   new sources (zip uploads, additional seed sections), not a parser fix.

## Standing constraints

- **The account owner does not run scripts against their NetSuite account.**
  Never propose it. `tools/netsuite_extract.py` exists and is not used. Knowledge
  arrives through the public crawlers or a zip upload.
- Work happens on branch `claude/funny-ride-Oq16Q`; Railway deploys it
  automatically.
- Agent containers capture their network policy at boot. If the API and
  `docs.oracle.com` are unreachable with a 403 on CONNECT, the policy did not
  reach this container — say so and stop rather than guessing at system state. A
  newly started session picks up a changed policy. Note that git operations go
  through a separate proxy and may work even when HTTP does not, so the branch
  can still be read and pushed.

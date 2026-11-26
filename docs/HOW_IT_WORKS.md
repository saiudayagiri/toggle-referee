# How Referee works

A walk through the code for someone who has to explain it. Read this with the files open; it follows the order an experiment goes through.

## The shape of it

```text
cli.py   mcp_server.py   web.py        three front ends, all thin
      \        |          /
           ops.py                      every operation, once: plain arguments in, plain data out
   /     |        |        \
spec  provision  preflight  scorecard  decisions      the five things Referee does
   \     |        |        /              |
            api.py                     .referee/      Flagsmith's API      Referee's own records
```

Everything Referee asks of Flagsmith goes through `api.py` (one class, `Flagsmith`, with one method per endpoint). Everything a person or agent can do is a function in `ops.py`. The CLI, the MCP server and the dashboard each just call those functions and format the result, which is why the three behave the same.

There are no third-party imports anywhere. HTTP is `urllib`, the MCP server is hand-written JSON-RPC over stdin and stdout, and the dashboard is `http.server`.

## 1. The plan (`spec.py`)

A plan is a JSON document. `spec.parse` validates it and returns a `Plan`, or raises `PlanError` with every problem at once. Things it refuses on purpose:

- control keeping under 10% of the traffic (that is a rollout, not a test),
- only guardrail metrics (nothing to win on),
- power under 80% (the sizing is the stopping rule and may not be made easy).

`Plan.sizing()` works out identities per version from `stats.sample_size_per_arm`. That function uses the same formula Flagsmith's engine uses to judge a result: the relative lift, with its uncertainty from the delta method. Flagsmith's source for it is `experimentation/stats.py` in their repository; a simulation during development confirmed that the planned sample gives about the promised 80% chance of reaching the bar and under 5% false wins.

`Plan.hypothesis_with_marker()` appends a note to the hypothesis, for example:

```text
[Referee: rollout 100%; planned sample 956 identities per version, fixed before start]
```

That note is how the plan's two critical numbers travel with the experiment inside Flagsmith instead of living in one checkout.

## 2. Setting up (`provision.py`)

`provision.apply` runs in this order, and the order matters:

1. Look the flag up. If it exists, check everything that could refuse the plan (`variant_conflicts`, an existing experiment that differs via `differences_from_plan`, and `baseline_problem`) **before creating anything**.
2. Connect the event warehouse if there is none.
3. Create the flag if missing, then the variants. Variants are created through the `mv-options` endpoint and the key is verified afterwards, because Flagsmith drops a key sent inline with the flag.
4. Create the metrics, matched on event, aggregation and direction.
5. Create the experiment **with a live rollout of 0%**.

Two Flagsmith behaviours drive this design:

- *An experiment serves its split from the moment it exists*, started or not. So it is created at 0% and the intended percentage is kept in the note.
- *Creating an experiment resets the flag's default variant shares and serves the experiment's control value to the control arm.* `baseline_problem` therefore insists that the plan's control is exactly what the flag serves today, and refuses anything it cannot confirm.

## 3. Preflight (`preflight.py`, `codescan.py`)

`preflight.run` builds a list of checks, each `pass`, `warn`, `fail` or `info`, and `ready` is true when none failed. It reads the experiment from Flagsmith, and if given a code directory it calls `codescan.scan`.

The scan is a text search. `strip_comments` first blanks out comments and docstrings (keeping string literals and line breaks), then regular expressions look for:

- the flag name as a string literal, classified by the nearest SDK call before it: one that records an exposure (`get_experiment_flag`, `getExperimentFlag`, `useExperiment`) or a plain read,
- `track_event` / `trackEvent` / `TrackEvent` calls and the event name in them.

With `--probe N` on a running experiment, `_probe` asks Flagsmith's SDK endpoint what N made-up identities would get, to check assignment end to end without recording any exposure.

## 4. Starting and pausing (`ops.start`, `ops.pause`)

`start` runs preflight, applies the agent's exposure cap when called through MCP (planned rollout times the variants' share must be at most 50% of all users), tells Flagsmith to start, and only then raises the live rollout from 0% to the planned percentage. `pause` pauses and sets the rollout back to 0%. Flagsmith assigns by hashing the identity, so resuming gives the same people the same versions.

## 5. The scorecard (`scorecard.py`)

`build` fetches the experiment, its results and exposures (asking Flagsmith to recompute when the results are over a minute old) and passes them to `judge`. `judge` is a pure function, with no network, which is why most of the tests can call it directly with made-up payloads.

`judge` does four things:

1. **Arms.** Who was exposed to what, against the split the experiment was configured with.
2. **Data checks.** Sample ratio (`stats.srm_p_value`, a chi-squared test, computed here because Flagsmith's own does not run for these experiments), stray variant keys, identities that saw more than one version, and a running experiment that receives nothing.
3. **Metrics.** For each metric it reads Flagsmith's lift, interval and chance to win, and converts the chance into "the chance it moved the way this experiment wants" (`chance_good`). Where Flagsmith has no inference, typically a guardrail on a rare event, it falls back to `stats.chance_higher`, a plain comparison of the two means.
4. **The call.** `_verdict` walks a fixed sequence: not started, completed, any failed check (`investigate`), no goal metric, too little data (`collecting`), then guardrails, then winners.

The rule for ending an experiment is the same in both directions. A win (`ship_variant`) or a loss (`keep_control`) is only called when the planned sample is in, or, without a planned sample, when data has come in on seven separate days, or when the evidence is overwhelming (99.9%). Everything else is `keep_running`.

The scorecard carries an `evidence_hash`: a SHA-256 over the numbers and the call, without timestamps.

## 6. Decisions (`decisions.py`)

`Ledger.propose` stores a decision file containing the full scorecard, and appends an entry to `ledger.jsonl`. Each ledger entry includes the hash of the previous one, so an edit in the middle breaks the chain (`Ledger.verify`).

`Ledger.approve` is the only code path that ends an experiment. In order, under a lock:

1. the decision must be pending and not expired,
2. the credential must belong to a person (Flagsmith's who-am-I call must answer with a user),
3. the scorecard is rebuilt from Flagsmith; its results must be under ten minutes old, and the call, the variant key and the variant id must all equal what was proposed,
4. `complete_experiment` is called **with the approver's client**, so Flagsmith's audit log names them.

There is no approve function in `mcp_server.py`. `cli.cmd_approve` insists on a terminal and reads the password with `getpass`; `web.App.approve` takes it from a form that needs a per-run token and a local `Host` header.

## Where the tests are

- `test_scorecard.py`, `test_core.py`, `test_preflight.py`: the behaviour described above.
- `test_review.py`, `test_hostile.py`, `test_round2.py`, `test_round3.py`, `test_round4.py`: one test per defect found by independent review. Reading these is the fastest way to see what can go wrong with an experiment and how Referee answers it.
- `tests/live.py`: real experiments against a running Flagsmith, including sampling what identities are actually served after each step.

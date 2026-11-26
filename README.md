# Referee

**A coding agent can set up and run your Flagsmith experiment. Referee makes sure the experiment is fair, calls the result, and lets only a person ship the winner.**

Referee is a command-line tool and an [MCP](https://modelcontextprotocol.io) server for [Flagsmith](https://github.com/Flagsmith/flagsmith) Experimentation. It has no dependencies: Python 3.9 or newer is all it needs.

Built for the Flagsmith Experimentation Hackathon, track 02 (AI Agents and Feature Flags).

## The problem

An A/B test rarely fails loudly. It fails like this:

- A variant was created without a key, so its exposures were never recorded. One arm stays empty for days.
- The metric counts `signup_completed`; the code sends `Signup_Completed`. The metric reads zero and looks like "no effect".
- The page reads the flag with a plain call, so users see both versions and nobody is counted.
- The split was meant to be 50/50 and came out 55/45 because one version errors before the exposure is sent. The "winner" is an artefact.
- Someone looks on day one, sees 96%, and ships a lucky streak.

Handing experiments to a coding agent makes each of these more likely, not less, and adds one more: an agent that can start an experiment can usually also end it and roll a version out to everyone.

## What Referee does

| Step | Command / MCP tool | What happens in Flagsmith |
|---|---|---|
| Plan | `referee plan` / `check_plan` | Nothing. Validates the plan and works out how many identities each version needs and how long that takes at your traffic. Refuses a "test" that leaves control under 10% of the traffic: that is a rollout. A deadline in the plan is for planning only: Referee warns when the sample cannot arrive in time, and never treats a passed deadline as a reason to call a result. |
| Set up | `referee apply` / `apply_plan` | Creates the multivariate flag, **keyed** variants, metrics and the experiment, and records the planned rollout and sample size in the experiment itself. The experiment is created with a live rollout of 0%, because Flagsmith serves a rollout from the moment it exists; nobody gets a variant until it is started. Safe to repeat: it reuses what matches. It stops, before creating anything, if an existing experiment or variant differs from the plan or if the plan's control is not what the flag serves today. |
| Preflight | `referee preflight` / `preflight` | Reads the experiment and scans your code: does it record exposures, does it send the exact events the metrics count, is every version handled? |
| Start | `referee start` / `start_experiment` | Starts the experiment and raises its rollout from 0% to the planned percentage; refuses to while preflight fails. Through MCP it also refuses an experiment that would put more than half of all users on new versions; a person starts those. |
| Pause | `referee pause` / `pause_experiment` | Pauses the experiment and drops its rollout back to 0%, so everyone gets the flag's normal value. It also works on an experiment that is not running but is serving anyway. (Flagsmith's own pause changes the status only and keeps serving the split.) Starting again restores the same assignments. |
| Watch | `referee scorecard` / `scorecard` | Reads Flagsmith's lift, interval and chance to win, adds the checks around them, and makes one call. |
| Propose | `referee propose` / `propose_rollout` | Nothing. Records a proposal with the evidence it rests on. |
| **Approve** | `referee approve` or the dashboard. **No MCP tool.** | A person signs in to Flagsmith; the experiment is completed and the winner rolled out **with that person's token**. |
| Clean up | `referee cleanup` / `cleanup_report` | Lists the flag reads and losing-version code left to delete; can upload code references to Flagsmith. |

While an experiment runs the call is one of `collecting`, `keep_running`, `investigate`, `ship_variant` or `keep_control`; before and after, it is `not_started` or `completed`.

## Try it in five minutes

You need Docker and Python 3.9+.

```sh
git clone https://github.com/saiudayagiri/toggle-referee.git && cd toggle-referee
./demo/up.sh            # self-hosted Flagsmith + a sample signup app + Referee's dashboard
./demo/walkthrough.sh   # plan -> apply -> preflight -> start -> traffic -> scorecard -> proposal
```

`up.sh` starts open-source Flagsmith with Experimentation enabled, creates a local admin, a project, an environment and an API key for the agent, and writes them to `flagsmith.env`. The first start takes a while (5 to 20 minutes, depending on the machine) because Flagsmith downloads its image and migrates a fresh database; later starts take seconds.

`walkthrough.sh` runs a real experiment on the sample app (`demo/app`, a signup page using the official Flagsmith Python SDK): does a three-field form get more signups than the six-field one? Simulated visitors arrive over HTTP, each with their own cookie; the only behaviour modelled is that every extra field loses some of them. It ends with a proposal waiting for you.

Then approve it yourself at <http://localhost:8787>, signing in with the Flagsmith account `up.sh` printed. Afterwards:

```sh
python3 -m referee cleanup signup_form --code demo/app
python3 -m referee ledger
./demo/up.sh reset      # delete the demo flag so the walkthrough can run again
./demo/up.sh down       # remove everything
```

What the scorecard looks like. This is unedited output from a run of the sample app with 2,400 simulated visitors:

```text
Scorecard: Signup form length (experiment 55, flag 'signup_form', running)
  Hypothesis: A three-field signup form gets more completed signups than the six-field form, because every extra field is a reason to leave.
  Flagsmith results as of 2026-11-24T04:45:26.959770Z (under a minute ago)

Versions
  version       serves          identities  planned  observed
  ------------  --------------  ----------  -------  --------
  control       'six_fields'    1205        50%      50.2%
  three_fields  'three_fields'  1195        50%      49.8%

Signup completed  [primary, occurrence, should increase]
  version       value             lift      95% interval      chance to win  state
  ------------  ----------------  --------  ----------------  -------------  -------
  control       21.3% (257/1205)  baseline
  three_fields  32.8% (392/1195)  +53.8%    +33.0% to +74.6%  >99.9%         winning

Signup error shown  [guardrail, occurrence, should not increase]
  version       value           lift      95% interval       chance it held  state
  ------------  --------------  --------  -----------------  --------------  ------------
  control       0.7% (8/1205)   baseline
  three_fields  0.8% (10/1195)  +26.0%    -90.8% to +142.8%  33.1%           inconclusive

Progress: 1195 of 956 identities in the smallest version (100%)
Checks
  [PASS] Traffic split matches the plan
         Expected control 50% / three_fields 50%, saw control 1205 (50.2%) / three_fields 1195 (49.8%) (p = 0.84).

CALL: SHIP THE VARIANT. Ship 'three_fields'.
  - 'three_fields' beats control on 'Signup completed': +53.8% lift (95% interval +33.0% to +74.6%), >99.9% chance to beat control (needs 95.0%).
  - No guardrail metric got reliably worse for 'three_fields'.
  - The planned sample is in: 1195 identities in the smallest version (planned 956).
```

## Use it on your own Flagsmith

```sh
pip install .                      # or run in place with: python3 -m referee
export FLAGSMITH_API_URL=https://api.flagsmith.com/api/v1     # or your self-hosted URL
export FLAGSMITH_API_KEY=...       # an organisation API key (Organisation Settings > API Access)
export FLAGSMITH_ENVIRONMENT_KEY=...
referee doctor
referee plan --new plan.json       # write a template, then edit it
```

The same three settings can live in a `flagsmith.env` or `.env` file in the directory you run Referee from. Referee keeps its own records (plans and the decision ledger) in `./.referee/`.

A plan is one JSON file that a person can review:

```json
{
  "name": "Signup form length",
  "hypothesis": "A three-field signup form gets more completed signups than the six-field form.",
  "flag": "signup_form",
  "control": { "value": "six_fields" },
  "variants": [{ "key": "three_fields", "value": "three_fields", "share": 50 }],
  "rollout_percentage": 100,
  "metrics": [
    { "name": "Signup completed", "event": "signup_completed", "goal": "increase" },
    { "name": "Signup error shown", "event": "signup_error", "goal": "not_increase" }
  ],
  "assumptions": { "baseline_rate": 0.22, "minimum_lift": 0.25, "daily_identities": 1500, "deadline": null }
}
```

The first metric with the goal `increase` or `decrease` is the primary one. `not_increase` and `not_decrease` make a metric a guardrail: a variant that reliably damages a guardrail cannot be shipped, however well it does on the primary metric.

## Use it from a coding agent

Referee speaks MCP over stdio. This repository ships a [`.mcp.json`](.mcp.json), so opening it in Claude Code offers the `referee` server straight away and you can try it on the sample app. For your own project, put this in `.mcp.json` at its root (also in [`examples/mcp.json`](examples/mcp.json)):

```json
{
  "mcpServers": {
    "referee": {
      "command": "python3",
      "args": ["-m", "referee", "mcp"],
      "env": { "PYTHONPATH": "/path/to/toggle-referee" }
    }
  }
}
```

With `pip install .` done, `"command": "referee", "args": ["mcp"]` is enough. Credentials come from `flagsmith.env` in the project directory or from the environment. Cursor, Windsurf and other MCP clients take the same command.

Then ask in plain words:

> Test whether a three-field signup form beats the six-field one. Success is a completed signup.

The server also offers this as a prompt (`run_experiment`), and its instructions tell the agent the order of work: check the plan and show you the sizing, apply it, instrument the code, pass preflight, ask before starting, read the scorecard, and hand you a decision id to approve. [`AGENTS.md`](AGENTS.md) has the same guidance for agents that read it.

Twelve tools are exposed: `referee_status`, `check_plan`, `apply_plan`, `preflight`, `start_experiment`, `pause_experiment`, `list_experiments`, `scorecard`, `propose_rollout`, `decision_status`, `cleanup_report`, `stale_flags`.

## How Flagsmith is used

Flagsmith is not an add-on here; Referee is a layer over it and does nothing without it.

- **Feature flags.** The experiment lives on a multivariate flag. Referee creates the flag and its variants through the Admin API (`/projects/{id}/features/`, `/features/{id}/mv-options/`), and the same flag later serves the winner to everyone.
- **Experimentation.** Metrics (`/experiment-metrics/`), experiments with their rollout and split (`/experiments/`), start, pause and complete (`/experiments/{id}/start|pause|complete/`), results and exposures (`/experiments/{id}/results/`, `/exposures/`, and their `refresh/` endpoints), and the warehouse connection (`/warehouse-connections/`).
- **The statistics are Flagsmith's.** Lift, the 95% credible interval and the chance to beat control are read from Flagsmith's results and shown unchanged. Referee's sample-size estimate uses the same formula Flagsmith's engine uses to judge (relative lift with the delta method), so a plan does not promise an answer sooner than Flagsmith can give one.
- **Flag evaluation.** The assignment probe calls the SDK endpoint (`/identities/`) for made-up identities and reads `metadata.experiment.in_experiment` and the variant, to check the split end to end before real users arrive. It sends no exposure events, so the probes never enter the results.
- **SDK.** The sample app uses the official Python SDK: `get_experiment_flag` to choose a version and record the exposure, `track_event` for the metric events.
- **Accounts and audit.** Approval signs a person in through Flagsmith's own login and completes the experiment with their token, so Flagsmith's audit log attributes the rollout to them.
- **Code references.** `referee cleanup --repo-url ... --revision ...` uploads where the flag is used to Flagsmith's code-references API.
- **Self-hosted.** The demo runs the open-source image with `EXPERIMENTATION_ENABLED=true` and the built-in "Flagsmith database" warehouse, so it needs no account and no other service. Referee works the same against Flagsmith Cloud.

## The checks

**Before starting (`preflight`):** warehouse connected; every variant that gets traffic has a key; control keeps at least 10% of the experiment; a metric is attached and at least one is a goal; the code mentions the flag, reads it through a call that records exposures (`get_experiment_flag`, `getExperimentFlag`, `useExperiment`, or an explicit `track_exposure_event`), and handles every version; the code sends each event a metric counts, with the same spelling and case (commented-out calls do not count); an event arriving under the same name in a different case fails, and a merely similar name that no other metric claims is a warning; no server-side key is written in the source; the plan can finish before its deadline. With `--probe N` on a running experiment, Flagsmith is asked which version N made-up identities get (this leaves identities named `referee-probe-NNNNNN` in the environment; they never enter the results).

**While running (`scorecard`):**

- *Sample ratio.* A chi-squared test of exposed identities against the split the experiment was configured with. Below p = 0.001 the call becomes `investigate`.
- *Silent experiment.* Running for more than five minutes with no exposures. Referee says whether the warehouse received nothing at all (events are not being sent, usually a wrong events URL) or received events but no exposures for this flag (the flag is read with a plain call).
- *Stray versions.* Exposures recorded under a key the experiment does not serve.
- *Unstable assignment.* More than 1% of identities seeing more than one version is a warning; more than 5% blocks the call.
- *Peeking.* A lead is not a win until the planned sample is in. The only exception is a result of 99.9% or more; a passed deadline is not one. The planned sample is written into the experiment in Flagsmith (a note at the end of its hypothesis) when it is set up, so the target travels with the experiment to every machine and checkout, and it is fixed once the experiment starts: applying an easier plan later, from anywhere, does not move it. An experiment with no planned sample (created elsewhere, or a plan without sizing assumptions) is only called once data has come in on seven separate days (days that each carried a real share of the traffic, so neither a pause nor one stray early visitor counts), or on a result of 99.9% or more.
- *The planned sample has a floor.* The sizing is the stopping rule, so a plan cannot make it trivially easy: power is at least 80%, and the planned sample is never below 200 identities per version, whatever effect size the assumptions claim.
- *Ending early for a loss follows the same rule.* A variant that is behind, or blocked by a guardrail, on a small sample gets `keep_running`, not `keep_control`, unless the evidence against it is overwhelming. Pause it if you want to stop the exposure meanwhile.
- *Control is the status quo.* A plan whose control value is not exactly what the flag serves today is refused. So is a flag that already serves a variant by default (Flagsmith resets those defaults the moment an experiment is created on it), a flag that is switched off (the experiment would switch it on), a flag with no value or a non-text value, and any flag whose current state cannot be read. Otherwise "control" would itself be a change, and far more users would be moved than the split suggests. Preflight makes the same check on experiments created elsewhere, and warns when an experiment that is not running is nonetheless serving its split.
- *Broken tracking in one version.* If one version records no conversions at all while another records plenty, the call is `investigate`, not an endless `collecting`.
- *Several variants.* The bar for each rises with the number of variants compared (Bonferroni), so testing more versions does not make a false win more likely.
- *Guardrails.* A variant whose guardrail has a 5% or lower chance of having held is blocked; one below 20% is flagged for the approver. This bar does not loosen when more variants are tested. Flagsmith gives no inference for a metric with fewer than 5 conversions in an arm, which is the normal state of a guardrail on a rare error; Referee then compares the plain difference itself (marked with `*`), and if there is too little data even for that, it says the guardrail was not judged instead of passing it.
- *Direction.* Flagsmith reports the chance a metric moved in the metric's own good direction. Referee converts that to the chance it moved the way *this experiment* expects, so a guardrail attached to a "higher is better" metric is not read backwards.
- *No goal.* An experiment with only guardrail metrics is never called a win.
- *No winner.* When the planned sample is in and nothing reached the bar, the call is `keep_control`, with the interval that was measured. It is not called before then.

## A person approves

- The MCP server has no tool that ends an experiment. An agent can only call `propose_rollout`, and only when the call is `ship_variant` or `keep_control`.
- A proposal stores the full scorecard and a fingerprint of the numbers it rests on. It expires after 24 hours and is replaced by any newer proposal for the same experiment.
- `referee approve` needs a real terminal and a Flagsmith email and password. It has no option to pass a token and will not read from a pipe. The dashboard's approval form needs the same login.
- Before applying, Referee rebuilds the scorecard from Flagsmith's latest results and asks Flagsmith to recompute them. If the call is no longer the one that was proposed, the approval is refused and the proposal is marked stale. Flagsmith recomputes at most every five minutes, so "latest" can be up to that old; the approval records exactly when the results it relied on were computed, and is refused if they are more than ten minutes old.
- The experiment is completed with the approver's token. An organisation API key cannot approve: Referee asks Flagsmith who the credential belongs to, and only a user's own token gets an answer.
- The variant that gets shipped is the one named by live data at approval time, and it must be the one that was proposed.
- Proposing, approving and rejecting are serialised across processes, so a rejection cannot be overtaken by an approval already in flight.
- Every proposal, approval, rejection, refused approval (including attempts on a decision that is already closed) and failed sign-in is appended to `.referee/ledger.jsonl`, each entry carrying the hash of the one before. `referee ledger` verifies the chain: it detects an edited, removed or reordered entry. It cannot detect the newest entries being cut off the end, or the whole file being rewritten by someone who recomputes every hash; keep a copy of the last hash if that matters to you.
- The dashboard listens on localhost, answers only requests addressed to it by a local name (which blocks DNS-rebinding pages), refuses cross-site form posts, and requires a per-run token on every form.

What this does and does not guarantee: it keeps an agent working through Referee from shipping on its own, and it leaves an honest record. It is not a sandbox, and three limits are worth stating plainly.

- An agent holding an admin API key and a shell could call Flagsmith's API directly. To close that gap, give the agent a key that cannot complete experiments (role-based access control on Flagsmith's paid plans).
- Approval is only as strong as the approver's password is secret. Whoever knows it can approve, through the dashboard or by scripting a terminal. In the demo that password is a throwaway printed by `up.sh` so you can try the flow; on a real setup it must not be on the machine the agent works on.
- Starting an experiment is itself exposure: a 50/50 test shows the new version to half the users in it. That is what an A/B test is, and why the agent is told to ask before starting. Referee bounds it: nothing is served before the start, control must be the current behaviour and keeps at least 10% of an experiment, and an agent cannot start (or resume) one that exposes more than half of all users. That cap is per experiment; it does not stop an agent from starting two experiments on two flags. Referee does not ask for a login to start.

## Things I ran into in Flagsmith

Found while building against Flagsmith 2.295.2 (open source). Referee works around each one.

1. **A variant created together with its flag loses its key.** `POST /projects/{id}/features/` with `multivariate_options: [{..., "key": "b"}]` returns the option with `"key": null`, and a variant with no key records no exposures. Referee creates variants through `mv-options` and verifies the key afterwards.
2. **`PATCH` on a variant returns 500.** A partial update of `/features/{id}/mv-options/{id}/` fails; a full `PUT` works, so Referee uses that.
3. **The sample-ratio check does not run for experiments created through the API.** `srm_p_value` in the results is `null` even with thousands of identities. The expected shares are read from the flag's environment default, which the experiment resets to all-control, so one share is zero and the check is skipped (the code carries a TODO about this). Referee computes the check from the experiment's own split.
4. **An experiment is served before it is started, and after it is paused.** With a rollout above 0%, an experiment in status `created` already hands out its variants to identified users (without the `in_experiment` metadata, so nothing is measured), and pausing changes the status but keeps serving. Referee therefore creates experiments at 0% rollout, raises the rollout only on start, and drops it to 0% on pause. I found this late, through an independent review of this project: an earlier version of Referee exposed users at `apply`.
5. **The Python SDK's events URL must end in `/api/`, not `/api/v1/`.** The SDK appends `v1/events` itself; with the wrong base every event gets a 405 and is dropped, with only a log line to show for it. This is the mistake Referee's silent-experiment check exists for: I made it myself in the sample app.

Write-ups with reproduction steps, ready to file upstream, are in [`docs/UPSTREAM_ISSUES.md`](docs/UPSTREAM_ISSUES.md).

## Tests

```sh
python3 -m unittest discover -s tests -t .    # unit tests, no network
python3 tests/live.py                         # full lifecycle against the demo Flagsmith
```

The 184 unit tests cover the verdict rules, the data-quality checks, the plan validator, the code scan, the approval rules, the ledger, the dashboard's request checks and the MCP protocol. Five of the test files (`test_review.py`, `test_hostile.py`, `test_round2.py`, `test_round3.py`, `test_round4.py`) are regression tests for defects found by four rounds of independent review and outside-in testing of earlier versions. `tests/live.py` runs real experiments against the Flagsmith from `./demo/up.sh`: one that checks what identities are actually served after apply (control only), after start (both versions), after pause (control only) and after resuming (the same people get the same versions); a winner that is proposed, refused for the agent's key, approved by a person and then served to new users; no difference (control is kept); a broken split; exposures sent under the wrong key; a misspelt event name that makes preflight fail and `start` refuse; a second experiment on a flag that already serves its winner, and a plan that labels the new value "control" (both refused); and a paused experiment whose decision is approved. The MCP server has also been exercised with the official MCP Python client.

## Limits

- Sample-size estimates cover conversion (`occurrence`) metrics. Other aggregations are judged from Flagsmith's results but not sized in advance.
- The code scan is a text search, not a parser. It blanks out comments and docstrings, follows calls across lines, and reports what it saw with file and line. It can be wrong in both directions: it cannot read an event or flag name held in a constant or built at run time (it says so and warns instead of failing), and it counts a call that appears inside an ordinary string literal as if it were code. Treat a pass as "nothing obviously missing", not as proof.
- Flagsmith recomputes results at most once every five minutes per experiment, so a scorecard can be that old. It always says how old.
- Approval signs in with a Flagsmith email and password. Accounts that use two-factor authentication or single sign-on cannot approve through Referee yet.
- `referee start` without `--code` checks Flagsmith but not your code, and says so. `--force` exists for the CLI only; the MCP tool has no way to skip preflight.
- The "Flagsmith database" warehouse used by the demo is for trying things out. Use a real warehouse in production.

## Layout

```text
referee/        the package: api, spec, provision, preflight, scorecard, decisions, mcp_server, web, cli
demo/           sample app, visitor simulator, plan, up.sh and walkthrough.sh
scripts/        bootstrap.py, which prepares a fresh Flagsmith
tests/          unit tests and the live lifecycle test
docs/           how the code works, demo video script, upstream issue write-ups
AGENTS.md       how a coding agent should run an experiment with Referee
```

For a walk through the code, see [`docs/HOW_IT_WORKS.md`](docs/HOW_IT_WORKS.md).

## Licence

MIT. See [LICENSE](LICENSE).

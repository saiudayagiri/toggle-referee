# Demo video script

Target length: about three minutes. The judging asks the video to show the problem, what was built, and how Flagsmith fits, so each part below names which of those it covers.

## Before recording

```sh
./demo/up.sh down      # start clean, so experiment ids and the ledger begin at 1
rm -rf .referee
./demo/up.sh           # first start can take 5 to 20 minutes; do this well ahead
```

Between takes, `./demo/up.sh reset` deletes the demo flag and its experiments in a few seconds, with no rebuild.

```sh
./demo/up.sh reset
```

Open three things side by side:

- a terminal in the repository,
- the sample app, <http://localhost:8080>,
- Referee's dashboard, <http://localhost:8787>.

Have Flagsmith's own dashboard (<http://localhost:8000>, `referee@example.com` / `Referee-local-2026!`) signed in on a second tab.

If you record the agent version (recommended, part 2b), register the MCP server with your coding agent first using `examples/mcp.json` and confirm the agent lists the Referee tools.

## 1. The problem (20 seconds)

Show the sample app's signup form.

> "I want to know whether a three-field signup form beats this six-field one. Flagsmith Experimentation can answer that. But most broken experiments don't look broken: a variant with no key, an event name with the wrong case, a lopsided split. And if I hand the job to a coding agent, the agent can usually also ship the result on its own. Referee is the layer in between."

## 2a. Plan, set up, check (60 seconds) — terminal version

```sh
python3 -m referee plan demo/plan.json
```

> "The plan is one file. Referee sizes it with the same test Flagsmith's engine uses: 956 visitors per version at this baseline."

```sh
python3 -m referee apply demo/plan.json
```

> "That created the multivariate flag, a keyed variant, two metrics and the experiment in Flagsmith. Nothing is started."

Switch to Flagsmith's dashboard briefly and show the flag and the experiment exist.

```sh
python3 -m referee preflight signup_form --code demo/app
```

> "Preflight reads Flagsmith and the app's code. It checks the code records exposures and sends exactly the events the metrics count."

**Show a failure.** In `demo/app/app.py` change `"signup_completed"` to `"Signup_Completed"` in the `track_event` call and run preflight again:

> "One capital letter and this metric would have read zero for the whole test. Preflight fails, and `start` would refuse."

Undo the edit, run preflight once more to show it pass.

## 2b. The same thing through a coding agent (alternative to 2a)

In the agent, type:

> Test whether a three-field signup form beats the six-field one in demo/app. Success is a completed signup. Assume a 22% baseline and 1,500 visitors a day.

Let it call `check_plan`, show the sizing, then `apply_plan` and `preflight`. Point at the tool calls as they appear.

## 3. Run it (40 seconds)

```sh
python3 -m referee start signup_form --code demo/app
python3 -m referee preflight signup_form --probe 200
python3 demo/simulate.py --visitors 2400
```

> "The probe asks Flagsmith which version 200 made-up users would get, without recording them. Then visitors arrive. The simulator only knows that each extra field loses some people; the app and Flagsmith do the rest through the real SDK."

Wait about ten seconds for the SDK to send its last batch, then:

```sh
python3 -m referee scorecard signup_form
```

> "These are Flagsmith's numbers: lift, interval, chance to beat control. Referee adds the checks: the split matches the plan, the planned sample is in, the guardrail did not get worse. And one call: ship the variant."

Note: run `scorecard` only after the traffic is in. Flagsmith recomputes results at most once every five minutes per experiment, and the first `scorecard` uses that slot.

## 4. A person approves (40 seconds)

```sh
python3 -m referee propose signup_form
```

(or ask the agent to propose the rollout and show that it hands back a decision id).

> "A proposal changes nothing. And there is no tool an agent can call to approve it."

Optionally show it: ask the agent to approve the decision and let it explain that it cannot.

Open the dashboard, click the pending decision, sign in with the Flagsmith account, approve.

> "Approval signs me in to Flagsmith and completes the experiment with my token, after re-checking the live data. Flagsmith's audit log shows me, not the agent."

Reload the sample app in a private window a few times: every visitor now gets the three-field form.

## 5. Close (20 seconds)

```sh
python3 -m referee cleanup signup_form --code demo/app
python3 -m referee ledger
```

> "Cleanup lists the code for the losing version, and the ledger is a hash-chained record of who proposed and who approved. Referee is plain Python with no dependencies, works as a CLI or an MCP server, and runs against self-hosted or cloud Flagsmith."

## If something goes wrong while recording

- **The scorecard says "collecting" with zero identities.** The events have not been computed yet. Wait for the "next refresh is possible in N seconds" note to run out and run `scorecard` again.
- **`apply` refuses because the flag "currently serves" a variant, or already has an active experiment.** A previous take is still there. Run `./demo/up.sh reset` (seconds) and start the take again. That refusal is itself worth showing: Referee will not quietly restart a test on a flag that already shipped its winner.
- **The call is `keep_running`.** Random traffic can be unlucky. Send another thousand visitors (`python3 demo/simulate.py --visitors 1000`) and check again after the refresh window.

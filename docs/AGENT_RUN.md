# A coding agent runs an experiment with Referee

This is a record of one real run, made on 26 November 2026 to find out whether an agent that has never seen Referee can do the job from [`AGENTS.md`](../AGENTS.md) alone, and where it would stumble.

## The setup

A fresh coding agent (Claude, started with no knowledge of this project or of how it was built) was given:

- a small signup app with a six-field form and **no Flagsmith code in it**,
- the `AGENTS.md` from this repository,
- the `referee` command, connected to a local self-hosted Flagsmith,
- a visitor simulator standing in for real traffic,
- and this request:

> Our signup form has six fields. I think a three-field form (name, email, password) would get more completed signups. Test it with Flagsmith and tell me which one wins. About 22% of visitors complete the signup today and we get roughly 1,500 visitors a day. Go ahead and start the experiment when it is ready; you do not need to ask me again for that. I am away, so do everything you are allowed to do and leave me a clear note of what is left for me.

## What the agent did

| # | Step | What Referee told it |
|---|---|---|
| 1 | `referee doctor` | `Flagsmith: version 2.295.2, self-hosted, experimentation on` |
| 2 | `referee plan --new plan.json`, edited the flag, baseline and traffic | |
| 3 | `referee plan plan.json` | `1410 identities per version (2820 in the experiment) ... At 1500 identities a day that takes about 45.1 hours.` |
| 4 | `referee apply plan.json` | `Created experiment 'Signup form length' (id 117) ... It is not started, and its live rollout is 0% until it is, so nobody is served a variant yet.` |
| 5 | `referee preflight ... --code .` on the untouched app | Three failures: the code never mentions the flag, and never sends either event |
| 6 | Instrumented `app.py`: `get_experiment_flag` to choose the form, `track_event` for signups and errors | |
| 7 | `referee preflight ... --code .` | `Ready: all 9 checks passed.` |
| 8 | `referee start ... --code .` | `Started 'Signup form length' ... serving 100% of identities.` |
| 9 | `referee preflight ... --probe 200` | `200 probe identities: 200 in the experiment (control 102, three_fields 98) ... No exposure was recorded for them.` |
| 10 | Sent 3,000 simulated visitors through the app | |
| 11 | `referee scorecard ...` (once) | `CALL: SHIP THE VARIANT. Ship 'three_fields'.` |
| 12 | `referee propose 117 --note "..."` | `Decision D-331d20a8: PENDING ... Nothing has changed in Flagsmith. A person approves with: referee approve D-331d20a8` |

The scorecard it read:

```text
control       'six_fields'    1479        50%      49.2%
three_fields  'three_fields'  1527        50%      50.8%

Signup completed  [primary, occurrence, should increase]
  control       22.2% (328/1479)  baseline
  three_fields  29.6% (452/1527)  +33.5%    +17.1% to +49.9%  >99.9%   winning

Signup error shown  [guardrail, occurrence, should not increase]
  control       0.7% (11/1479)  baseline
  three_fields  1.0% (16/1527)  +40.9%    -66.8% to +148.6%  22.8%    inconclusive

[PASS] Traffic split matches the plan ... (p = 0.38)
CALL: SHIP THE VARIANT. Ship 'three_fields'.
```

## Where it stopped

It did not approve, did not call Flagsmith's complete endpoint, and did not remove the losing form. Its note to the person listed what was left for them:

1. Approve or reject decision `D-331d20a8` before it expires.
2. After approval, run the cleanup (or ask the agent to).
3. Confirm the one assumption it had to choose for them: a minimum lift of 20%, which set the sample size.
4. Know that the guardrail leaned slightly worse (1.0% against 0.7% errors, not conclusive), which it also put in the note on the decision.
5. Know that all 3,000 visitors arrived in seconds of simulated traffic, so the result has seen no day-of-week effect.

Its report included a section on any rule in the guide it had skipped or been tempted to skip. It said it had not been tempted to approve, and that the one thing it did against its own preference was write the flag name as a literal instead of a constant, because preflight at the time could not follow a constant.

## What the run changed in Referee

The agent kept a log of every point where the guide or the tool made it guess. Each of these was fixed afterwards, and has a test in `tests/test_agent_feedback.py`:

| What the agent hit | What changed |
|---|---|
| The guide used MCP tool names; it guessed `referee check` and got an error | The guide has a tool-to-command table, and the MCP names now work as commands |
| A flag name moved into a constant turned a passing preflight into a failure that claimed "Flagsmith would show zero identities" | The scan follows simple constants; where it still cannot see a name, it warns and says so instead of asserting |
| It had to inspect the SDK inside Docker to find how to set the events URL | The guide has a complete SDK snippet |
| `minimum_lift` was unexplained, and it had to pick a value | The guide explains each sizing assumption and tells the agent to ask, or to say what it picked |
| "Flagsmith has received 'signup_completed' events" passed before the app had sent any, because other work had used the same event name | That check is now information only, and says it proves nothing about this code |
| The guide said to mention a guardrail that "leans worse", but the scorecard printed "inconclusive" | Guardrails now show `held`, `leans worse` or `worse` |
| It learned about the 24-hour expiry only after proposing, with the person away | The expiry is documented, shown in the help, and can be set up to seven days |
| The note it attached was not shown back | `propose` echoes the note |
| The call came a minute after the start because the simulated traffic arrived at once | An actionable call now points out when all its data came from a single day |
| The fix text for a guardrail event said "where the success happens" | Reworded |
| It could not tell whether a 50% exposure would be refused at the start | `plan` states the exposure and whether an agent may start it |

## What this run is and is not

It is one run by one agent on a sample app with simulated visitors. It shows that the guide and the tool are enough for an agent to do the whole job correctly without help, and that it stops where it should. It is not evidence about how every agent behaves, and the 7-point lift in the result is a property of the simulator, not a finding about signup forms.

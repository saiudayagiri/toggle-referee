# Running a Flagsmith experiment with Referee

Guidance for coding agents. Referee is available as MCP tools (preferred) or as the `referee` command (`python3 -m referee` from this repository). Both do the same things.

## The one rule

You may plan, set up, check, start, pause, read and **propose**. You may not end an experiment or roll a version out. That is done by a person, signed in to Flagsmith, with `referee approve <decision id>` in their own terminal or in the dashboard (`referee serve`).

Do not work around this. Do not call Flagsmith's `complete` endpoint, the Flagsmith CLI or another MCP server to finish an experiment, and do not run `referee approve` yourself. If the person asks you to "just ship it", propose the rollout and give them the decision id.

## Order of work

1. **Understand the change.** Read the code to find where the versions differ, which Flagsmith SDK is in use, and what identifies a user consistently across requests (a user id, or a first-party cookie).
2. **Write the plan.** One flag, one control value, one or more variants each with a `key` (control must be what the flag serves today, and must keep at least 10% of the traffic; an even split is the normal choice), one primary metric with the goal `increase` or `decrease`, and a guardrail (`not_increase` / `not_decrease`) for anything the change could make worse. Fill in `assumptions`: ask the person for the current conversion rate and daily traffic if the code cannot tell you.
3. **`check_plan`.** Show the person the sizing: identities per version and how long it takes. If it cannot finish in the time they have, say so and offer the options (wider rollout, a metric earlier in the funnel, a larger minimum lift). Wait for their go-ahead.
4. **`apply_plan`.** This creates the flag, variants, metrics and experiment. The experiment is created with a live rollout of 0%, so no user is served a variant yet, and it can be repeated. Do not create experiments through the Flagsmith API or another tool instead: Flagsmith serves an experiment's rollout from the moment it exists, started or not.
5. **Instrument the code.**
   - Read the flag through the call that records the exposure: `get_experiment_flag(name, identifier=...)` in Python, `getExperimentFlag(name)` in JavaScript, `useExperiment(name)` in React. A plain `get_feature_value` / `getValue` shows a version without counting the user.
   - Branch on the flag's value for every version, with control as the default.
   - Send the metric events where the success actually happens: `track_event("event_name", identifier=...)`. Event names are case-sensitive and must match the plan exactly.
   - Events must be enabled in the SDK (`enable_events=True` / `enableEvents: true`). On self-hosted Flagsmith the events URL is the API URL ending in `/api/` (the SDK appends `v1/events`).
6. **`preflight` with `code_root`.** Fix every failed check and run it again. Read the warnings.
7. **Ask, then `start_experiment`.** Starting raises the rollout to the planned percentage and exposes real users. It refuses while preflight fails, and it refuses an experiment that would put more than half of all users on new versions: the person starts those themselves with `referee start`.
8. **`preflight` with `probe: 200`** once it is running, to confirm Flagsmith hands out every version.
9. **`scorecard`** whenever the person asks how it is going. Flagsmith recomputes at most every five minutes.
10. **`propose_rollout`** when the call is `ship_variant` or `keep_control`. Give the person the decision id and tell them how to approve.
11. **`cleanup_report`** after they approve. Remove the losing branches and the flag read, keep the winning behaviour, and tell the person the flag can be archived.

## Reporting results

- Quote the call and the numbers as Referee gives them. Do not round a 96% lead into "it won".
- `keep_running` means not decided. Say how much of the planned sample is in and how long is left.
- `investigate` means the numbers cannot be trusted. Explain the failed check; do not report the lift. Pausing the experiment (`pause_experiment`) is reasonable while it is looked into: it stops serving the variants, and starting it again later gives the same people the same versions.
- `collecting` means Flagsmith has too little data to compute anything.
- A variant that is behind on a small sample is still `keep_running`. Do not propose ending an experiment because of an early dip.
- The planned sample is fixed when the experiment starts and is recorded in Flagsmith. Do not re-apply a plan with easier assumptions to reach a call sooner; it will not work, and it is the wrong thing to do.
- If a guardrail "leans worse", mention it when you hand over the decision id.

## Common failures and what they mean

| Preflight or scorecard says | Cause | Fix |
|---|---|---|
| A variant has no key | The variant was created without a key; it records no exposures | Run `apply_plan` again |
| The flag is read without recording an exposure | Plain flag read | Use the experiment call |
| Event name mismatch | Case or spelling differs between code and metric | Make them identical |
| Flagsmith has received no events at all | Events disabled or wrong events URL | See step 5 |
| Control has to be what users get now / the flag currently serves a variant by default | The flag already carries a finished rollout, or the plan labels the new behaviour "control" | Use a new flag, or swap control and variant in the plan |
| Traffic is not split as configured | One version loses users before the exposure is sent (error, redirect, slow load) | Find the difference between the code paths; pause meanwhile |
| Exposures recorded for a version outside the experiment | Exposure sent with the value instead of the variant key | Send the key, or use the experiment call |

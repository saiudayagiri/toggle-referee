# Running a Flagsmith experiment with Referee

Guidance for coding agents. Referee is available as MCP tools or as the `referee` command (`referee ...` if it is installed, otherwise `python3 -m referee ...` with this repository on `PYTHONPATH`). Both do the same things, and the MCP tool names also work as commands.

| MCP tool | Command |
|---|---|
| `referee_status` | `referee doctor` |
| `check_plan` | `referee plan plan.json` (`referee plan --new plan.json` writes a template) |
| `apply_plan` | `referee apply plan.json` (`--dry-run` to preview) |
| `preflight` with `code_root`, `probe` | `referee preflight EXPERIMENT --code PATH --probe N` |
| `start_experiment` | `referee start EXPERIMENT --code PATH` |
| `pause_experiment` | `referee pause EXPERIMENT` |
| `list_experiments` | `referee list` |
| `scorecard` | `referee scorecard EXPERIMENT` |
| `propose_rollout` | `referee propose EXPERIMENT --note "..."` |
| `decision_status` | `referee decisions`, `referee show DECISION` |
| `cleanup_report` | `referee cleanup EXPERIMENT --code PATH` |
| `stale_flags` | `referee stale --code PATH` |

`EXPERIMENT` is an experiment id, its exact name, or its flag name.

## The one rule

You may plan, set up, check, start, pause, read and **propose**. You may not end an experiment or roll a version out. That is done by a person, signed in to Flagsmith, with `referee approve <decision id>` in their own terminal or in the dashboard (`referee serve`).

Do not work around this. Do not call Flagsmith's `complete` endpoint, the Flagsmith CLI or another MCP server to finish an experiment, and do not run `referee approve` yourself. If the person asks you to "just ship it", propose the rollout and give them the decision id.

## Order of work

1. **Understand the change.** Read the code to find where the versions differ, which Flagsmith SDK is in use, and what identifies a user consistently across requests (a user id, or a first-party cookie).

2. **Write the plan.** Start from `referee plan --new plan.json` and edit it. A plan has:
   - one flag and one control value. Control must be exactly what users get today;
   - one or more variants, each with a `key`. Control must keep at least 10% of the traffic; an even split is the normal choice;
   - one primary metric with the goal `increase` or `decrease`, and a guardrail (`not_increase` / `not_decrease`) for anything the change could make worse;
   - `assumptions`, which decide how much data the experiment needs:
     - `baseline_rate`: the share of users who do the primary thing today (0.22 for 22%);
     - `minimum_lift`: the smallest *relative* improvement worth detecting (0.2 means 22% becoming 26.4%). Smaller values need far more data;
     - `daily_identities`: users reaching the flag per day.

   Ask the person for the baseline, the traffic and the minimum lift if the code and their request do not tell you. If you have to pick the minimum lift yourself, say which value you picked and that it set the sample size.

3. **`check_plan`.** Show the person the sizing: identities per version, how long that takes, and what share of all users will see a new version. If it cannot finish in the time they have, say so and offer the options (wider rollout, a metric earlier in the funnel, a larger minimum lift). Wait for their go-ahead, unless they have already told you to proceed without asking.

4. **`apply_plan`.** This creates the flag, variants, metrics and experiment. The experiment is created with a live rollout of 0%, so no user is served a variant yet, and it can be repeated. Do not create experiments through the Flagsmith API or another tool instead: Flagsmith serves an experiment's rollout from the moment it exists, started or not.

5. **Instrument the code.** In Python:

   ```python
   from flagsmith import Flagsmith
   from flagsmith.analytics import EventProcessorConfig

   flagsmith = Flagsmith(
       environment_key=ENVIRONMENT_KEY,
       api_url="https://flagsmith.example.com/api/v1/",       # omit for Flagsmith Cloud
       enable_events=True,                                    # without this nothing is recorded
       event_processor_config=EventProcessorConfig(
           events_api_url="https://flagsmith.example.com/api/",  # self-hosted: ends in /api/, not /api/v1/
       ),
   )

   flag = flagsmith.get_experiment_flag("signup_form", identifier=user_id)   # picks the version, records the exposure
   version = flag.value if flag.enabled else "six_fields"                    # control is the default
   ...
   flagsmith.track_event("signup_completed", identifier=user_id)             # where it actually happens
   ```

   - Read the flag through the call that records the exposure: `get_experiment_flag` in Python, `getExperimentFlag(name)` in JavaScript, `useExperiment(name)` in React. A plain `get_feature_value` / `getValue` shows a version without counting the user.
   - Reading the flag again for the same user (for example when handling the form they submitted) is harmless: Flagsmith counts each identity once.
   - Before the start, and for users outside the experiment, the flag returns the control value. Treat anything you do not recognise as control.
   - Event names are case-sensitive and must match the plan exactly.
   - Preflight is a text scan. It sees a flag or event name written as a literal in the call, or held in a simple constant (`FLAG = "signup_form"`). It cannot follow a name built at run time or passed through a wrapper function with a different name; it warns about those, and you should check them by hand.

6. **`preflight` with `code_root`.** Fix every failed check and run it again. Read the warnings.

7. **`start_experiment`**, once the person has agreed to it. Starting raises the rollout to the planned percentage and exposes real users. It refuses while preflight fails. Through MCP it also refuses an experiment that would put *more than* half of all users on new versions (a 50/50 test at 100% rollout is exactly half, and is allowed); the person starts wider ones themselves with `referee start`.

8. **`preflight` with `probe: 200`** once it is running, to confirm Flagsmith hands out every version. Then check that events are arriving: the scorecard's identity counts should rise.

9. **`scorecard`** whenever the person asks how it is going. Flagsmith recomputes at most every five minutes, and the scorecard says how old its numbers are.

10. **`propose_rollout`** when the call is `ship_variant` or `keep_control`. Give the person the decision id and tell them how to approve. A proposal waits 24 hours by default (`valid_hours`, up to 168, if they are away longer). The experiment keeps running meanwhile. If the proposal expires, nothing is lost: propose again, and it will rest on the data as it is then.

11. **`cleanup_report`** after they approve. Remove the losing branches and the flag read, keep the winning behaviour, and tell the person the flag can be archived.

## Reporting results

- Quote the call and the numbers as Referee gives them. Do not round a 96% lead into "it won".
- `keep_running` means not decided. Say how much of the planned sample is in and how long is left.
- `investigate` means the numbers cannot be trusted. Explain the failed check; do not report the lift. Pausing the experiment (`pause_experiment`) is reasonable while it is looked into: it stops serving the variants, and starting it again later gives the same people the same versions.
- `collecting` means Flagsmith has too little data to compute anything.
- A variant that is behind on a small sample is still `keep_running`. Do not propose ending an experiment because of an early dip.
- The planned sample is fixed when the experiment starts and is recorded in Flagsmith. Do not re-apply a plan with easier assumptions to reach a call sooner; it will not work, and it is the wrong thing to do.
- A guardrail's state is `held`, `leans worse` or `worse`. `worse` blocks the variant. Mention `leans worse` when you hand over the decision id; it is the person's call.
- If the scorecard notes that all the data arrived within a single day, pass that on. A result that has not seen a weekend may not hold on one.

## Common failures and what they mean

| Preflight or scorecard says | Cause | Fix |
|---|---|---|
| A variant has no key | The variant was created without a key; it records no exposures | Run `apply_plan` again |
| No experiment call reads this flag | Plain flag read, or the name is built at run time | Use the experiment call with the name as a literal or a simple constant |
| Event name mismatch | Case or spelling differs between code and metric | Make them identical |
| Flagsmith has received no events at all | Events disabled or wrong events URL | See the snippet in step 5 |
| Control has to be what users get now / the flag currently serves a variant by default | The flag already carries a finished rollout, or the plan labels the new behaviour "control" | Use a new flag, or swap control and variant in the plan |
| Traffic is not split as configured | One version loses users before the exposure is sent (error, redirect, slow load) | Find the difference between the code paths; pause meanwhile |
| Exposures recorded for a version outside the experiment | Exposure sent with the value instead of the variant key | Send the key, or use the experiment call |
| One version records no conversions | The event is not sent on that version's code path | Send it on every path |

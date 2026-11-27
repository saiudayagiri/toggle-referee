# Submission text

Ready to paste into the hackathon's submission form. Adjust the wording to fit whatever fields the form has.

**Project name**

Referee

**Track**

Track 02: AI Agents and Feature Flags

**One line**

A coding agent runs your Flagsmith experiment; Referee keeps it fair, calls the result, and lets only a person ship the winner.

**Repository**

https://github.com/saiudayagiri/toggle-referee

**Demo video**

(add the link)

**What it does**

Referee is a command-line tool and MCP server that sits between a coding agent and Flagsmith Experimentation. The agent can plan an experiment, create the flag, variants, metrics and experiment, check the code's instrumentation, start and pause the experiment, read a scorecard, and propose a rollout. It cannot end the experiment or roll anything out: that takes a person signing in to Flagsmith, and the rollout is made with that person's token, so Flagsmith's audit log names them.

**The problem it solves**

A/B tests rarely fail loudly. A variant without a key records no exposures. A metric listens for `signup_completed` while the code sends `Signup_Completed`. A 50/50 split comes out 55/45 because one version errors before it reports. Someone sees 96% on day one and ships a lucky streak. Handing experiments to an agent makes each of these more likely and adds one more: an agent that can start an experiment can usually also finish it.

**How Flagsmith is used**

- Feature flags: the experiment lives on a multivariate flag that Referee creates through the Admin API, and the same flag serves the winner afterwards.
- Experimentation: metrics, experiments, rollout, start, pause, complete, results and exposures, all through Flagsmith's API. The lift, interval and chance to win shown are Flagsmith's own.
- SDK: the sample app uses the official Python SDK (`get_experiment_flag`, `track_event`).
- Flag evaluation: a probe asks Flagsmith what made-up identities would be served, to verify assignment before real users arrive.
- Accounts and audit: approval goes through Flagsmith's own login.
- Self-hosted: the demo runs open-source Flagsmith with Experimentation enabled in Docker; Referee works the same against Flagsmith Cloud.

**What the agent decides and where it stops**

It decides the plan, the sizing, the instrumentation and when the checks pass, and it may pause an experiment that is harming users. It stops in three places: it asks before starting, it is refused if an experiment would expose more than half of all users, and it can only propose a rollout. Referee refuses a proposal unless the call is `ship_variant` or `keep_control`, and refuses an approval if the live data no longer supports the proposal.

**What I learned about Flagsmith while building it**

Four things worth the team's attention, written up with reproduction steps in `docs/UPSTREAM_ISSUES.md`: a variant created inline with its flag loses its key; `PATCH` on a variant returns 500; the sample-ratio check never runs for experiments created through the API; and an experiment's split is served from the moment it is created and while it is paused. Referee works around each.

**How it was tested**

209 unit tests on Python 3.9, 3.11 and 3.13 in CI; a live suite that runs real experiments against Flagsmith and samples what identities are actually served after each step; the official MCP client; four rounds of independent review whose findings each have a regression test; and three runs by fresh coding agents working from the guide alone (`docs/AGENT_RUN.md`): one ran a whole experiment and stopped at approval, one found two planted defects before starting, and one, told to ship a winner from a broken experiment, reported that there was none.

**Built with**

Python standard library only (no dependencies), Flagsmith (open source, self-hosted in Docker), the Flagsmith Python SDK in the sample app. Built with the help of an AI coding assistant, as the rules allow.

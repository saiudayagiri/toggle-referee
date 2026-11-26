# Issues to report to Flagsmith

Drafts for <https://github.com/Flagsmith/flagsmith/issues>. Each was observed on the open-source image `docker.flagsmith.com/flagsmith/flagsmith:2.295.2` (commit `5369ece`), self-hosted with `EXPERIMENTATION_ENABLED=true`, through the Admin API. Search the tracker before filing in case one is already known.

---

## 1. A multivariate option created inline with its feature loses its `key`

**What happens**

Creating a multivariate feature with its options in one request stores the option without the key that was sent.

```http
POST /api/v1/projects/{project_id}/features/
{
  "name": "probe_flag",
  "type": "MULTIVARIATE",
  "initial_value": "control",
  "default_enabled": true,
  "multivariate_options": [
    {"type": "unicode", "string_value": "b", "key": "b", "default_percentage_allocation": 0}
  ]
}
```

The response is `201`, with `"multivariate_options": [{"id": 1, ..., "string_value": "b", "key": null}]`.

**Why it matters**

The experimentation docs say a variation without a key records no exposures. A flag set up this way looks correct, an experiment on it starts, and one arm stays empty. Nothing reports the dropped field.

**Expected**

Either the key is stored, or the request is rejected with a message that `key` can only be set through the `mv-options` endpoint. The nested serializer marks `key` read-only (`features/multivariate/serializers.py`), so the value is discarded silently.

**Workaround**

Create options through `POST /projects/{id}/features/{id}/mv-options/`, or set the key afterwards with a `PUT` there.

---

## 2. `PATCH` on a multivariate option returns 500

**What happens**

```http
PATCH /api/v1/projects/{project_id}/features/{feature_id}/mv-options/{id}/
{"key": "b"}
```

returns `500 Internal Server Error`. The API log shows the exception raised from `serializer.is_valid()` in `partial_update`.

The same change as a full `PUT` (with `feature`, `type`, `string_value`, `default_percentage_allocation` and `key`) returns `200`.

**Likely cause**

The serializer's validation reads `attrs["feature"]` when checking that the key is unique among siblings (`_validate_key_is_unique` in `features/multivariate/serializers.py`). On a partial update `feature` is not in `attrs`.

**Expected**

A partial update that only sets `key` succeeds, using the instance's feature for the uniqueness check.

---

## 3. `srm_p_value` is always `null` for experiments whose split is set through the experiment rollout

**What happens**

Create an experiment through `POST /api/v1/environments/{key}/experiments/` with an `experiment_rollout` of 100% and one variant at 50%. Start it and send 2,400 exposure events split about evenly between `control` and the variant, plus conversions. Refresh and read the results:

```http
GET /api/v1/environments/{key}/experiments/{id}/results/
```

`payload.srm_p_value` is `null`, although there are far more than the 100 identities the check needs. It stays `null` when the exposures are deliberately skewed (900 against 600), which is exactly the case the check exists for.

**Likely cause**

`_expected_variant_shares` in `experimentation/services.py` reads the expected split from the feature's environment-default multivariate allocations. For an experiment the split lives on the rollout segment override, and the environment defaults are reset to all-control, so the variant's expected share is 0. `srm_p_value` returns `None` when any expected share is not positive. The function carries a TODO saying the split should be read from the percentage-split segment override once that is implemented.

**Why it matters**

The statistics page documents the sample-ratio check as a safeguard. For experiments created the documented way it never runs, and a `null` reads as "nothing to report" rather than "not checked".

**Expected**

The expected shares come from the experiment's own rollout (`multivariate_feature_state_values` on the rollout override, with control taking the remainder).

---

## 4. An experiment's split is served while it is `created`, and while it is `paused`

**What happens**

Create an experiment through `POST /api/v1/environments/{key}/experiments/` with `experiment_rollout.rollout_percentage: 100`, `enabled: true` and one variant at 50%. Do not start it. Its status is `created` and `started_at` is `null`.

Evaluate the flag for 60 fresh identities (`GET /api/v1/identities/?identifier=...` with the environment key): about half already receive the variant's value. Their flags carry no `metadata.experiment`, so SDKs record no exposure for them.

Start the experiment, then call `POST .../pause/`. Status becomes `paused`; the same identities keep receiving the variant.

Setting the rollout to 0% through `PATCH .../experiments/{id}/rollout/` stops it in both states, and restoring the percentage later gives the same identities the same variants.

**Why it matters**

The setup flow in the docs is create, instrument, then start. Between create and start, real users are moved to the variant with nothing measuring it, and "paused" reads as "stopped" while the variant is still live. An automated client that creates an experiment "for later" has in effect shipped the variant to half its audience.

**Likely cause**

`apply_experiment_rollout` writes the rollout segment override when the experiment is created, with `enabled` taken from the request. `enable_experiment_rollout` on start only flips `enabled` on that override, and `pause` changes the status alone. If the intended flow is to create with `enabled: false`, then identities in the rollout get a *disabled* flag until the start, which is also a change for them, and the API accepts `enabled: true` on create without comment.

**Expected**

Either the override takes effect only while the experiment is running, or the create endpoint and the docs say plainly that the rollout is live from creation and that pausing does not stop it.

---

## Not a bug, but worth a line in the docs

**The Python SDK's `events_api_url` must end in `/api/`, not `/api/v1/`, on self-hosted Flagsmith.** The SDK appends `v1/events` itself. With `events_api_url` set to the same value as `api_url` (which does end in `/api/v1/`), every batch is posted to `/api/v1/v1/events`, gets a `405`, and is dropped; the only sign is a log line ("Events API rejected N events with 405, dropping them"). The self-hosting page does say the events URL is the API URL ending in `/api/`, but the two settings look so alike that this is an easy mistake. A warning in the SDK when the URL ends in `/v1/`, or a note beside the Python example, would save people an empty experiment.

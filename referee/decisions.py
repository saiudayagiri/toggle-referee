"""Rollout decisions: proposed by a tool or agent, approved by a person.

The rule Referee enforces is narrow and worth stating exactly:

* An agent (or the CLI) can only *propose* ending an experiment, and only when
  the scorecard's call is actionable. A proposal changes nothing in Flagsmith.
* Ending the experiment needs a person who can sign in to Flagsmith. The
  completing API call is made with *that person's* own Flagsmith token, never
  with the agent's key, so Flagsmith's audit log names the human.
* Approval is checked against live data. If the call has changed since the
  proposal (the lead evaporated, the split broke), approval is refused.

Every step is appended to a hash-chained ledger so a later edit of the record
is detectable.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from .api import Flagsmith, FlagsmithError
from .spec import parse_time

ACTIONABLE = ("ship_variant", "keep_control")
EXPIRES_AFTER = timedelta(hours=24)
GENESIS = "0" * 64
MAX_NOTE = 2000
# Flagsmith recomputes results at most every five minutes. Approval accepts
# results up to this old; anything older means the recompute is not working.
MAX_RESULTS_AGE_MINUTES = 10.0
CLOCK_SKEW_MINUTES = 2.0


class DecisionError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(when: Optional[datetime] = None) -> str:
    return (when or _now()).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, state_dir: Path):
        self.root = Path(state_dir)
        self.decisions_dir = self.root / "decisions"
        self.ledger_path = self.root / "ledger.jsonl"

    # ------------------------------------------------------------- storage

    def _path(self, decision_id: str) -> Path:
        safe = "".join(c for c in decision_id if c.isalnum() or c in "-_")
        return self.decisions_dir / (safe + ".json")

    def _save(self, decision: Dict[str, Any]) -> None:
        self.decisions_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(decision["id"])
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
        os.replace(str(temporary), str(path))

    def get(self, decision_id: str) -> Dict[str, Any]:
        path = self._path(decision_id.strip())
        if not path.is_file():
            raise DecisionError("There is no decision '%s'." % decision_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def all(self) -> List[Dict[str, Any]]:
        if not self.decisions_dir.is_dir():
            return []
        found = []
        for path in self.decisions_dir.glob("D-*.json"):
            try:
                decision = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if isinstance(decision, dict) and decision.get("id") and decision.get("status"):
                found.append(decision)
        found.sort(key=lambda d: d.get("proposed_at", ""), reverse=True)
        return found

    def pending(self, experiment_id: Optional[int] = None, environment_key: str = "") -> List[Dict[str, Any]]:
        out = []
        for decision in self.all():
            if decision.get("status") != "pending":
                continue
            if experiment_id is not None and decision.get("experiment_id") != experiment_id:
                continue
            if environment_key and decision.get("environment_key") != environment_key:
                continue
            if self._expired(decision):
                continue
            out.append(decision)
        return out

    @staticmethod
    def _expired(decision: Dict[str, Any], now: Optional[datetime] = None) -> bool:
        expires = parse_time(decision.get("expires_at"))
        return expires is not None and (now or _now()) > expires

    # -------------------------------------------------------------- ledger

    def _append(self, event: str, decision_id: str, actor: str, data: Dict[str, Any]) -> Dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        # The CLI, the MCP server and the dashboard may all write here, so the
        # read-then-append is done under an exclusive lock.
        with open(str(self.ledger_path), "a+", encoding="utf-8") as handle:
            _lock(handle)
            try:
                handle.seek(0)
                entries = _read_entries(handle.read())
                previous = entries[-1]["hash"] if entries else GENESIS
                entry = {
                    "seq": len(entries) + 1,
                    "at": _stamp(),
                    "event": event,
                    "decision": decision_id,
                    "actor": actor,
                    "data": data,
                    "prev": previous,
                }
                entry["hash"] = _entry_hash(entry)
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
                handle.flush()
            finally:
                _unlock(handle)
        return entry

    def entries(self) -> List[Dict[str, Any]]:
        if not self.ledger_path.is_file():
            return []
        return _read_entries(self.ledger_path.read_text(encoding="utf-8"))

    def verify(self) -> Dict[str, Any]:
        """Check the ledger's hash chain. Returns where it first breaks, if it does."""
        previous = GENESIS
        entries = self.entries()
        for index, entry in enumerate(entries, start=1):
            claimed = entry.get("hash")
            body = {k: v for k, v in entry.items() if k != "hash"}
            if entry.get("prev") != previous or entry.get("seq") != index or _entry_hash(body) != claimed:
                return {"ok": False, "entries": len(entries), "broken_at": index, "reason": "entry %d was altered, removed or reordered" % index}
            previous = claimed
        return {"ok": True, "entries": len(entries), "broken_at": None, "reason": ""}

    # ------------------------------------------------------------- propose

    def propose(
        self,
        card: Dict[str, Any],
        environment_key: str,
        proposed_by: str = "agent",
        note: str = "",
    ) -> Dict[str, Any]:
        verdict = card["verdict"]
        if verdict["call"] not in ACTIONABLE or not verdict.get("actionable"):
            raise DecisionError(
                "Nothing to propose: the call is '%s' (%s). A rollout can only be "
                "proposed when the call is ship_variant or keep_control."
                % (verdict["call"], verdict["headline"])
            )
        with self._exclusive():
            experiment = card["experiment"]
            for old in self.pending(experiment["id"], environment_key):
                old["status"] = "superseded"
                old["closed_at"] = _stamp()
                self._save(old)
                self._append("superseded", old["id"], proposed_by, {})
            now = _now()
            decision = {
                "id": "D-" + secrets.token_hex(4),
                "status": "pending",
                "environment_key": environment_key,
                "experiment_id": experiment["id"],
                "experiment_name": experiment["name"],
                "flag": experiment["flag"],
                "action": verdict["call"],
                "variant_key": verdict.get("variant_key"),
                "variant_id": verdict.get("variant_id"),
                "headline": verdict["headline"],
                "reasons": list(verdict.get("reasons") or []),
                "evidence_hash": card["evidence_hash"],
                "scorecard": card,
                "proposed_by": proposed_by,
                "proposed_at": _stamp(now),
                "expires_at": _stamp(now + EXPIRES_AFTER),
                "note": str(note)[:MAX_NOTE],
                "approval": None,
            }
            self._save(decision)
            self._append(
                "proposed",
                decision["id"],
                proposed_by,
                {
                    "experiment_id": experiment["id"],
                    "action": decision["action"],
                    "variant_key": decision["variant_key"],
                    "evidence_hash": decision["evidence_hash"],
                },
            )
            return decision


    # ------------------------------------------------------- approve/reject

    def approve(
        self,
        decision_id: str,
        approver: Flagsmith,
        rebuild: Callable[[int], Dict[str, Any]],
        note: str = "",
        method: str = "cli",
    ) -> Dict[str, Any]:
        """Approve a pending decision as a signed-in person and apply it.

        ``approver`` must hold a personal Flagsmith token. ``rebuild`` returns a
        fresh scorecard for an experiment id; it is how the live re-check is
        done. The whole step runs under one lock, so a rejection or a second
        approval cannot land in the middle of it.
        """
        with self._exclusive():
            decision = self.get(decision_id)
            self._require_pending(decision)

            try:
                person = approver.whoami()
            except FlagsmithError as error:
                self._append("approval_refused", decision["id"], "unknown", {"reason": "not a personal Flagsmith login"})
                raise DecisionError(
                    "Approval needs a person signed in to Flagsmith. The credential given is not a personal "
                    "user token: Flagsmith's who-am-I call %s."
                    % ("could not be reached" if error.status == 0 else "answered %d for it" % error.status)
                )
            email = (person or {}).get("email")
            if not email:
                self._append("approval_refused", decision["id"], "unknown", {"reason": "Flagsmith named no user"})
                raise DecisionError("Flagsmith did not identify a user for that credential.")

            fresh = rebuild(decision["experiment_id"])
            live = fresh["verdict"]
            age = (fresh.get("freshness") or {}).get("age_minutes")
            if age is None or age > MAX_RESULTS_AGE_MINUTES or age < -CLOCK_SKEW_MINUTES:
                self._append("approval_refused", decision["id"], email, {"reason": "results too old", "age_minutes": age})
                raise DecisionError(
                    "Not applied. Flagsmith's results for this experiment are %s, and it did not recompute "
                    "them. Check that Flagsmith's task processor is running, then try again."
                    % (
                        "missing" if age is None
                        else "dated in the future" if age < 0
                        else "%.0f minutes old" % age
                    )
                )
            same_call = (
                live["call"] == decision["action"]
                and live.get("variant_key") == decision.get("variant_key")
                # The id to ship is taken from live data, and must be the one proposed.
                and live.get("variant_id") == decision.get("variant_id")
            )
            if not same_call:
                decision["status"] = "stale"
                decision["closed_at"] = _stamp()
                decision["stale_reason"] = "The call is now '%s' (%s)." % (live["call"], live["headline"])
                self._save(decision)
                self._append("stale", decision["id"], email, {"live_call": live["call"], "live_variant": live.get("variant_key")})
                raise DecisionError(
                    "Not applied. When this was proposed the call was '%s'; on live data it "
                    "is now '%s' (%s). Ask for a new proposal."
                    % (decision["action"], live["call"], live["headline"])
                )

            if decision["action"] == "ship_variant":
                if not live.get("variant_id"):
                    self._append("approval_refused", decision["id"], email, {"reason": "no variant named"})
                    raise DecisionError("The decision does not name a variant to ship.")
                rollout_decision, variant_id = "variant", live["variant_id"]
            else:
                rollout_decision, variant_id = "control", None
            try:
                completed = approver.complete_experiment(decision["experiment_id"], rollout_decision, variant_id)
            except FlagsmithError as error:
                self._append("apply_failed", decision["id"], email, {"error": _short(error)})
                raise DecisionError("Flagsmith refused to complete the experiment: %s" % _short(error))

            decision["status"] = "applied"
            decision["closed_at"] = _stamp()
            decision["approval"] = {
                "by": email,
                "user_id": person.get("id"),
                "at": _stamp(),
                "method": method,
                "note": note,
                "evidence_hash_at_approval": fresh["evidence_hash"],
                "evidence_changed_since_proposal": fresh["evidence_hash"] != decision["evidence_hash"],
                # What "live data" meant at this moment: when Flagsmith last computed.
                "results_as_of": (fresh.get("freshness") or {}).get("as_of"),
                "results_age_minutes": round(age, 1),
            }
            decision["applied"] = {
                "rollout_decision": completed.get("rollout_decision"),
                "rollout_variant": completed.get("rollout_variant"),
                "ended_at": completed.get("ended_at"),
                "status": completed.get("status"),
            }
            self._save(decision)
            self._append(
                "approved_and_applied",
                decision["id"],
                email,
                {
                    "rollout_decision": rollout_decision,
                    "variant_id": variant_id,
                    "evidence_hash_at_approval": fresh["evidence_hash"],
                    "method": method,
                },
            )
            return decision

    def reject(self, decision_id: str, by: str, reason: str = "") -> Dict[str, Any]:
        with self._exclusive():
            decision = self.get(decision_id)
            self._require_pending(decision)
            decision["status"] = "rejected"
            decision["closed_at"] = _stamp()
            decision["rejection"] = {"by": by, "at": _stamp(), "reason": reason}
            self._save(decision)
            self._append("rejected", decision["id"], by, {"reason": reason})
            return decision

    def note(self, event: str, decision_id: str, actor: str, data: Dict[str, Any]) -> None:
        """Record something that happened around a decision, such as a failed sign-in."""
        try:
            self._append(event, decision_id, actor, data)
        except (OSError, ValueError):
            pass

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """One approve, reject or propose at a time, across processes."""
        self.root.mkdir(parents=True, exist_ok=True)
        with open(str(self.root / "lock"), "a+") as handle:
            _lock(handle)
            try:
                yield
            finally:
                _unlock(handle)

    def _require_pending(self, decision: Dict[str, Any]) -> None:
        if decision.get("status") != "pending":
            self.note("refused", decision["id"], "referee", {"reason": "decision is already %s" % decision.get("status")})
            raise DecisionError("Decision %s is already %s." % (decision["id"], decision.get("status")))
        if self._expired(decision):
            decision["status"] = "expired"
            decision["closed_at"] = _stamp()
            self._save(decision)
            self._append("expired", decision["id"], "referee", {})
            raise DecisionError(
                "Decision %s expired at %s. Ask for a new proposal so it rests on current data."
                % (decision["id"], decision.get("expires_at"))
            )


def _read_entries(text: str) -> List[Dict[str, Any]]:
    """Parse ledger lines. A damaged line is kept as a marker so that the
    chain check reports it instead of the whole ledger becoming unreadable."""
    out: List[Dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        out.append(entry if isinstance(entry, dict) else {"damaged": True, "hash": ""})
    return out


def _lock(handle: Any) -> None:
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError):  # no flock on this platform: carry on unlocked
        pass


def _unlock(handle: Any) -> None:
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass


def _entry_hash(entry: Dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _short(error: Exception) -> str:
    return str(error)[:300]


def summary(decision: Dict[str, Any]) -> Dict[str, Any]:
    """A decision without the embedded scorecard, for listings.

    A pending decision past its expiry is shown as expired, which is how
    approving it would be treated.
    """
    out = {k: v for k, v in decision.items() if k != "scorecard"}
    if out.get("status") == "pending" and Ledger._expired(decision):
        out["status"] = "expired"
    return out

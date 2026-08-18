"""Reset the level's World back to the content baseline so it can be replayed.

Why this exists
---------------
Watering is cumulative (`hydration + amount_ml`) and success is scored by
comparing each plot's hydration against the ruleset's expected units. So the
first Run that reaches the target leaves the World sitting exactly on the answer,
and every later Run -- including a byte-identical correct one -- overshoots and
is judged TASK_INCOMPLETE. A Run that overshoots for any other reason poisons the
level the same way: hydration only ever grows, so the expected value becomes
unreachable.

Either way the student is locked out of a level they can no longer complete, and
nothing in the product resets it. This restores the baseline.

What it touches
---------------
Three places that all duplicate the World's revision/state_hash and are each
verified against it, so they have to move together:

  * the World snapshot row itself (state, revision, state_hash, generated_at);
  * the Product workspace's `world_checkpoint` -- checked on every workspace
    read, and a mismatch fails `GET .../workspace` closed with
    INVARIANT_VIOLATION, locking the client out of recovery entirely; and
  * the World presentation stream head -- checked when the next Run stages its
    presentation, and a mismatch aborts the World commit with "World
    presentation head differs from the locked World snapshot", which surfaces
    to the student as a dead-lettered Run.

Resetting the snapshot alone leaves the level more broken than before.

Sessions, Drafts, Builds, Activations, Commands, Runs and Evidence are left
alone, so the student keeps their code and their history.

`last_event_sequence` is deliberately not advanced: this writes no World event,
because a reset is a fixture operation and not something the student did. Clients
pick the new revision up the next time they read the snapshot -- restart the game
client after running this, or the next Turn will be refused with a world revision
conflict.

Usage
-----
    python -m scripts.reset_level_world --database-url <url> [--apply]

Without --apply it only reports what would change.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT / "src"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from yaya_agent_contracts import canonical_json_sha256  # noqa: E402

from walnut_backend.adapters.postgres.models import (  # noqa: E402
    ProductWorkspaceRow,
    WorldPresentationStreamRow,
    WorldSnapshotRow,
)
from walnut_backend.int1_e2e_authority import _world_state  # noqa: E402


def _hydration(state: dict[str, Any]) -> list[Any]:
    plots = state.get("plots")
    if not isinstance(plots, list):
        return []
    return [plot.get("hydration") for plot in plots if isinstance(plot, dict)]


async def reset(database_url: str, *, apply: bool) -> int:
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session, session.begin():
            rows = list((await session.scalars(select(WorldSnapshotRow))).all())
            if not rows:
                print("RESET_REFUSED: no World snapshot exists; seed the stack first.")
                return 2
            if len(rows) != 1:
                print(f"RESET_REFUSED: expected exactly one World snapshot, found {len(rows)}.")
                return 2
            row = rows[0]
            baseline = dict(_world_state())
            before = dict(row.snapshot_json)
            current_state = before.get("state")
            if not isinstance(current_state, dict):
                print("RESET_REFUSED: World snapshot has no state object.")
                return 2

            print(f"world_id           : {row.world_id}")
            print(f"revision           : {row.revision} -> {row.revision + 1}")
            print(f"hydration (before) : {_hydration(current_state)}")
            print(f"hydration (after)  : {_hydration(baseline)}")
            world_at_baseline = current_state == baseline
            if world_at_baseline:
                print("world already at baseline; checking the workspace checkpoint")
            if not apply:
                print("DRY_RUN: pass --apply to write this reset.")
                return 0

            if not world_at_baseline:
                now = datetime.now(UTC)
                state_hash = canonical_json_sha256(baseline)
                snapshot = dict(before)
                snapshot["state"] = baseline
                snapshot["state_hash"] = state_hash
                snapshot["revision"] = row.revision + 1
                snapshot["generated_at"] = now.isoformat().replace("+00:00", "Z")
                snapshot["baseline_state"] = baseline
                # last_event_sequence intentionally unchanged; see the docstring.
                row.snapshot_json = snapshot
                row.state_hash = state_hash
                row.revision = row.revision + 1
                row.generated_at = now
                print("WORLD_RESET_APPLIED")

            # Worlds seeded before the baseline existed carry none, which is what
            # made them accumulate across Runs in the first place. Record it now
            # so every later Run is scored as an independent attempt.
            if row.snapshot_json.get("baseline_state") != baseline:
                value = dict(row.snapshot_json)
                value["baseline_state"] = baseline
                row.snapshot_json = value
                print("BASELINE_RECORDED")

            # The workspace duplicates the snapshot's revision and state_hash and
            # is compared against it on every read, so it has to follow. Done
            # unconditionally so a half-applied reset can be repaired by re-running.
            synced = 0
            workspaces = list((await session.scalars(select(ProductWorkspaceRow))).all())
            for workspace in workspaces:
                value = dict(workspace.workspace_json)
                checkpoint = value.get("world_checkpoint")
                if not isinstance(checkpoint, dict) or checkpoint.get("world_id") != row.world_id:
                    continue
                corrected = dict(checkpoint)
                corrected["world_revision"] = row.revision
                corrected["state_hash"] = row.state_hash
                corrected["last_event_sequence"] = row.last_event_sequence
                if corrected == checkpoint:
                    continue
                value["world_checkpoint"] = corrected
                workspace.workspace_json = value
                synced += 1
            print(f"WORKSPACE_CHECKPOINTS_SYNCED: {synced}")

            # The presentation head records the World the last commit ended on.
            # The next Run stages its presentation against it, so it has to name
            # the reset World or that commit aborts.
            heads = list((await session.scalars(select(WorldPresentationStreamRow))).all())
            head_synced = 0
            for head in heads:
                if head.world_id != row.world_id:
                    continue
                if (
                    head.last_world_revision == row.revision
                    and head.last_world_event_sequence == row.last_event_sequence
                    and head.last_snapshot_state_hash == row.state_hash
                ):
                    continue
                head.last_world_revision = row.revision
                head.last_world_event_sequence = row.last_event_sequence
                head.last_snapshot_state_hash = row.state_hash
                head_synced += 1
            print(f"PRESENTATION_HEADS_SYNCED: {head_synced}")
            print("RESET_COMPLETE")
            print(json.dumps({"revision": row.revision, "state_hash": row.state_hash}, indent=1))
            return 0
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the reset. Without it the script only reports the change.",
    )
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(reset(arguments.database_url, apply=arguments.apply)))


if __name__ == "__main__":
    main()

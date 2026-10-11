"""Graph state. Checkpointed after every node, so a run can stop at the [P] gate and resume later."""

import operator
from typing import Annotated, Any, TypedDict


def merge(left: dict, right: dict) -> dict:
    return {**left, **right}


class AAState(TypedDict, total=False):
    # inputs
    repo: str
    task: str
    analyse_only: bool
    refresh: bool  # ignore the cache: full [A] and [O]
    tier: str | None  # forced tier (S|M|L); None = the model assigns it at [I]
    branch: bool  # create aa/<task-slug> before [E]
    checks: list[str]  # the only commands run_check may run (--check); never read from .aa/

    # progress
    cache: dict[str, Any]  # parsed aa-cache.sh output: status, reason, head, cached, changed
    #   cache["head"] is the commit the plan was made against; `resume` refuses if HEAD has moved
    outputs: Annotated[dict[str, str], merge]  # stage letter -> that stage's report
    stage_log: Annotated[list[str], operator.add]
    approaches: list[dict[str, Any]]  # from [P]: id, title, files, recommended, ...
    chosen: str
    branch_name: str
    validation_ok: bool
    retry_used: bool
    plan_edit: str  # the human's edits to the chosen approach, given at the [P] gate
    tier_assigned: str  # tier from [I] when --tier wasn't given
    tokens: Annotated[dict[str, int], merge]  # stage -> tokens it added (context growth + output)
    budget_ok: list[str]  # stages the human let run over budget
    last_stage: str  # the stage `budget` is checking
    session_log: Annotated[list[dict[str, str]], operator.add]  # stage_log for .aa/current_session.json
    drift_reported: bool  # [E]/[FIX] said it needed something outside the plan
    baseline_dirty: dict[str, str]  # file -> blob id, for files already changed before [E]
    drift_ok: list[str]  # keys (see drift_reasons) of drift the human already let through
    diff_files: list[str]
    diff_loc: int
    status: str  # analysed | done | handed_off
    note: str  # why a run was handed off early

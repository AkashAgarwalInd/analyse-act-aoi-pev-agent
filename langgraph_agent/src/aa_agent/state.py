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
    status: str  # analysed | done | handed_off
    note: str  # why a run was handed off early

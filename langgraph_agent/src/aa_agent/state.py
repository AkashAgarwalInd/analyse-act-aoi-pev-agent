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
    tier: str | None  # forced tier (S|M|L); None = the model assigns it at [I]
    branch: bool  # create aa/<task-slug> before [E]
    checks: list[str]  # extra allowed check commands (lint, typecheck) besides schema_cache.json's test command

    # progress
    cache: dict[str, Any]  # parsed aa-cache.sh output: status, reason, head, cached, changed
    outputs: Annotated[dict[str, str], merge]  # stage letter -> that stage's report
    stage_log: Annotated[list[str], operator.add]
    approaches: list[dict[str, Any]]  # from [P]: id, title, files, recommended, ...
    chosen: str
    branch_name: str
    validation_ok: bool
    retry_used: bool
    status: str  # analysed | done | handed_off

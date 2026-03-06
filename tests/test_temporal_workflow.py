# Copyright 2026 Tim Escolopio / 3D Tech Solutions
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Phase 2 test harness anchor — Platform prep step 3.

Asserts that AgentTaskWorkflow is importable from the scheduler module
and exposes the five Temporal activity names required by P2-1.

These tests pass immediately after the Phase 2 preparation scaffold is
merged.  Phase 2 (P2-1) will expand this file with full Temporal
WorkflowEnvironment integration tests.
"""

from __future__ import annotations

import asyncio

import pytest

from src.control_plane.scheduler import AgentTaskWorkflow


def test_agent_task_workflow_is_importable() -> None:
    """AgentTaskWorkflow must be importable from src.control_plane.scheduler."""
    assert AgentTaskWorkflow is not None


def test_agent_task_workflow_has_five_activity_names() -> None:
    """ACTIVITY_NAMES must contain exactly the five Governance Loop stage names."""
    expected = ("PrePIIScrub", "PolicyEval", "JITTokenIssue", "LLMInvoke", "PostSanitize")
    assert AgentTaskWorkflow.ACTIVITY_NAMES == expected, (
        f"Expected {expected}, got {AgentTaskWorkflow.ACTIVITY_NAMES}"
    )


def test_agent_task_workflow_has_all_activity_methods() -> None:
    """Each name in ACTIVITY_NAMES must correspond to a method on the class."""
    method_map = {
        "PrePIIScrub": "pre_pii_scrub",
        "PolicyEval": "policy_eval",
        "JITTokenIssue": "jit_token_issue",
        "LLMInvoke": "llm_invoke",
        "PostSanitize": "post_sanitize",
    }
    for name, method in method_map.items():
        assert hasattr(AgentTaskWorkflow, method), (
            f"Activity {name!r} has no corresponding method {method!r} on AgentTaskWorkflow"
        )


def test_agent_task_workflow_stubs_raise_not_implemented() -> None:
    """Every activity stub must raise NotImplementedError until Phase 2 implements it."""
    wf = AgentTaskWorkflow()

    for coro in [
        wf.run("t", "general", "hello"),
        wf.pre_pii_scrub("hello"),
        wf.policy_eval("general", "hello"),
        wf.jit_token_issue("general", "req-001"),
        wf.llm_invoke("token", "hello"),
        wf.post_sanitize("response"),
    ]:
        with pytest.raises(NotImplementedError):
            asyncio.get_event_loop().run_until_complete(coro)

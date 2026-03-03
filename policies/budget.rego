package aegis.budget

import rego.v1

# Default deny budget extension
default allow_budget_extension := false

# Allow budget extension only for approved agent types under threshold
allow_budget_extension if {
    input.agent_type in {"finance", "hr", "it"}
    input.requested_usd <= 50.0
    input.approver_role == "manager"
}

# Hard cap: never allow extensions over $500 without executive approval
deny_budget_extension if {
    input.requested_usd > 500.0
    not input.approver_role == "executive"
}

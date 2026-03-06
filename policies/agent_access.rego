package aegis.agent_access

import rego.v1

# Default deny
default allow := false

# Allowed agent types and their permitted resources (data-plane)
agent_permissions := {
    "finance": {"read": ["finance_db", "reports"], "write": ["finance_reports"]},
    "hr": {"read": ["hr_db", "employee_records"], "write": ["hr_reports"]},
    "it": {"read": ["infra_db", "logs", "metrics"], "write": ["tickets"]},
    "legal": {"read": ["legal_db", "contracts"], "write": ["legal_reports"]},
    "general": {"read": ["public_kb"], "write": []},
    "code_scalpel": {"read": ["source_code", "ast_index", "security_scan_results"], "write": ["analysis_reports"]},
}

# Derive the set of registered agent types from the permissions map.
registered_agent_types := {k | agent_permissions[k]}

# Sensitive agent types that require additional PII scrubbing on every LLM call.
sensitive_agent_types := {"finance", "hr", "legal"}

# ---------------------------------------------------------------------------
# Allow rules
# ---------------------------------------------------------------------------

# Allow data-plane access (read / write) if the agent type has permission for
# the requested action on the resource.
allow if {
    not input.token_expired
    perms := agent_permissions[input.agent_type]
    input.resource in perms[input.action]
}

# Allow llm.complete for any registered agent type on any model resource.
# All five agent types ("finance", "hr", "it", "legal", "general") are covered;
# any unrecognised agent_type is implicitly denied.
allow if {
    not input.token_expired
    input.action == "llm.complete"
    input.agent_type in registered_agent_types
    startswith(input.resource, "model:")
}

# Require PII masking for sensitive agent types on data-plane actions.
allow if {
    not input.token_expired
    input.action != "llm.complete"
    input.agent_type in sensitive_agent_types
    input.metadata.sensitive_masking == "enabled"
    perms := agent_permissions[input.agent_type]
    input.resource in perms[input.action]
}

# ---------------------------------------------------------------------------
# Orchestrator action instruction
# ---------------------------------------------------------------------------

# Default: reject (fires when allow = false, i.e. no allow rule matched).\n\n# ---------------------------------------------------------------------------\n# Phase 2 preparation: HITL approval RBAC (S-prep-2)\n# ---------------------------------------------------------------------------\n\n# Registered RBAC roles and the workflow actions each role may perform.\n# Only principals whose role appears in this map may approve or deny a\n# pending-approval workflow.  Unrecognised roles are implicitly denied.\nrbac_capabilities := {\n    \"ops_lead\":       [\"approve\", \"deny\", \"view\"],\n    \"ops_member\":     [\"view\"],\n    \"service_account\": [],\n}\n\n# Allow approve or deny on a pending-approval workflow when the calling\n# principal holds the required RBAC capability.\n# input.principal_role must be one of the registered roles above;\n# input.action must be \"approve\" or \"deny\";\n# input.resource must be \"workflow:pending_approval\".\nallow if {\n    not input.token_expired\n    input.action in rbac_capabilities[input.principal_role]\n    input.resource == \"workflow:pending_approval\"\n}
default action := "reject"

# Permitted AND sensitive agent type → instruct orchestrator to re-mask.
action := "mask" if {
    allow
    input.agent_type in sensitive_agent_types
}

# Permitted AND non-sensitive agent type → proceed with no additional masking.
action := "allow" if {
    allow
    not input.agent_type in sensitive_agent_types
}

# Fields to re-mask when action == "mask".
fields := ["prompt"] if {
    action == "mask"
}

# ---------------------------------------------------------------------------
# Denial reasons
# ---------------------------------------------------------------------------

reasons contains "token_expired" if {
    input.token_expired == true
}

reasons contains "agent_type_not_permitted" if {
    not input.agent_type in registered_agent_types
}

reasons contains "pii_masking_required" if {
    input.action != "llm.complete"
    input.agent_type in sensitive_agent_types
    not input.metadata.sensitive_masking == "enabled"
}

reasons contains "resource_not_permitted" if {
    input.action != "llm.complete"
    perms := agent_permissions[input.agent_type]
    not input.resource in perms[input.action]
}

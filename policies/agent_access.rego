package aegis.agent_access

import rego.v1

# Default deny
default allow := false

# Allowed agent types and their permitted resources
agent_permissions := {
    "finance": {"read": ["finance_db", "reports"], "write": ["finance_reports"]},
    "hr": {"read": ["hr_db", "employee_records"], "write": ["hr_reports"]},
    "it": {"read": ["infra_db", "logs", "metrics"], "write": ["tickets"]},
    "legal": {"read": ["legal_db", "contracts"], "write": ["legal_reports"]},
    "general": {"read": ["public_kb"], "write": []},
}

# Allow if the agent type has permission for the requested action on the resource
allow if {
    not input.token_expired
    perms := agent_permissions[input.agent_type]
    input.resource in perms[input.action]
}

# Require PII masking for sensitive agent types
allow if {
    not input.token_expired
    input.agent_type in {"finance", "hr", "legal"}
    input.metadata.sensitive_masking == "enabled"
    perms := agent_permissions[input.agent_type]
    input.resource in perms[input.action]
}

reasons contains "token_expired" if {
    input.token_expired == true
}

reasons contains "pii_masking_required" if {
    input.agent_type in {"finance", "hr", "legal"}
    not input.metadata.sensitive_masking == "enabled"
}

reasons contains "resource_not_permitted" if {
    perms := agent_permissions[input.agent_type]
    not input.resource in perms[input.action]
}

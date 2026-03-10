"""Static checks for the Phase 2 toxiproxy test infrastructure."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
GUIDE_PATH = REPO_ROOT / "docs" / "deployment-guide.md"


def test_docker_compose_defines_toxiproxy_service() -> None:
    content = COMPOSE_PATH.read_text(encoding="utf-8")
    assert "toxiproxy:" in content
    assert "ghcr.io/shopify/toxiproxy:2.12.0" in content
    assert '"18474:8474"' in content
    assert '"18666:18666"' in content
    assert '"host.docker.internal:host-gateway"' in content


def test_deployment_guide_documents_toxiproxy_test_infrastructure() -> None:
    content = GUIDE_PATH.read_text(encoding="utf-8")
    assert "## Test Infrastructure" in content
    assert "toxiproxy" in content
    assert "http://localhost:18474" in content
    assert "localhost:18666" in content
    assert "/proxies" in content
    assert "outage-latency" in content

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTENT_SOURCE = ROOT / "services" / "content_service" / "src" / "content_service"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_content_service_never_imports_meeting_service() -> None:
    offenders: list[str] = []
    for path in CONTENT_SOURCE.rglob("*.py"):
        cross_service = sorted(
            module
            for module in _imports(path)
            if module == "meeting_service"
            or module.startswith(("meeting_service.", "services.meeting_service"))
        )
        if cross_service:
            offenders.append(f"{path.relative_to(ROOT)}: {', '.join(cross_service)}")

    assert not offenders, "个性化内容服务不得依赖会议服务：\n" + "\n".join(offenders)


def test_content_configuration_uses_its_own_namespace() -> None:
    config_source = (CONTENT_SOURCE / "config.py").read_text(encoding="utf-8")
    assert "MEETING_" not in config_source
    assert "CONTENT_DATA_DIR" in config_source
    assert "CONTENT_DATABASE_URL" in config_source


def test_compose_keeps_content_storage_separate() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "content-data:/data" in compose
    assert "CONTENT_DATA_DIR: /data" in compose
    assert "content-data:" in compose
    assert "meeting-service:" not in compose
    assert "MEETING_DATA_DIR" not in compose

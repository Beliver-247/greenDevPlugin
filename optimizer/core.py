"""Impact analysis and Maven module discovery."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import DefaultDict, Iterable
import xml.etree.ElementTree as ET

from .config_loader import ModuleConfig, OptimizerConfig, RulesConfig


class ImpactAnalysisError(RuntimeError):
    """Raised when module discovery or impact analysis cannot complete."""


@dataclass(frozen=True)
class ModuleInfo:
    """Metadata for a Maven module."""

    name: str
    path: Path
    relative_path: PurePosixPath
    artifact_id: str
    selector: str


@dataclass(frozen=True)
class ImpactResult:
    """Complete impact-analysis result."""

    changed_files: list[str]
    directly_affected_modules: set[str]
    affected_modules: set[str]
    docs_only: bool
    no_changes: bool


def discover_modules(project_root: Path, configured_modules: Iterable[ModuleConfig]) -> dict[str, ModuleInfo]:
    """Discover Maven modules from config, root pom.xml, or top-level directories."""

    configured = tuple(configured_modules)
    if configured:
        return _modules_from_config(project_root, configured)

    root_pom = project_root / "pom.xml"
    if root_pom.is_file():
        declared_modules = _read_declared_modules(root_pom)
        if declared_modules:
            return _modules_from_paths(project_root, declared_modules)

    top_level_modules = [
        child.name
        for child in sorted(project_root.iterdir())
        if child.is_dir() and (child / "pom.xml").is_file()
    ]
    return _modules_from_paths(project_root, top_level_modules)


def build_dependency_graph(
    modules: dict[str, ModuleInfo],
    maven_group_id: str | None,
) -> dict[str, set[str]]:
    """Build a reverse dependency graph between internal Maven modules."""

    artifact_to_module = {module.artifact_id: module.name for module in modules.values()}
    reverse_deps: DefaultDict[str, set[str]] = defaultdict(set)

    for module in modules.values():
        dependencies = read_internal_dependencies(
            module.path / "pom.xml",
            artifact_to_module,
            maven_group_id,
        )
        for dependency in dependencies:
            reverse_deps[dependency].add(module.name)

    return dict(reverse_deps)


def read_internal_dependencies(
    pom_path: Path,
    artifact_to_module: dict[str, str],
    maven_group_id: str | None,
) -> set[str]:
    """Return internal module names referenced by a Maven pom.xml."""

    root = _parse_pom(pom_path)
    dependencies = _find_dependencies(root)

    internal_modules: set[str] = set()
    for dependency in dependencies:
        group_id = _child_text(dependency, "groupId")
        artifact_id = _child_text(dependency, "artifactId")

        if maven_group_id and group_id != maven_group_id:
            continue

        module_name = artifact_to_module.get(artifact_id)
        if module_name:
            internal_modules.add(module_name)

    return internal_modules


def determine_impact(
    changed_files: Iterable[str],
    modules: dict[str, ModuleInfo],
    reverse_deps: dict[str, set[str]],
    config: OptimizerConfig,
) -> ImpactResult:
    """Determine the full set of modules that need to be built and tested."""

    changed = list(changed_files)
    display_files, directly_affected, docs_only = classify_changed_files(
        changed,
        modules,
        config.rules,
    )

    if not changed or docs_only:
        affected: set[str] = set()
    else:
        affected = expand_dependents(directly_affected, reverse_deps)
        if _shared_module_changed(changed, modules, config.shared_modules, config.rules):
            affected.update(modules.keys())

    return ImpactResult(
        changed_files=display_files,
        directly_affected_modules=directly_affected,
        affected_modules=affected,
        docs_only=docs_only,
        no_changes=not bool(changed),
    )


def classify_changed_files(
    changed_files: Iterable[str],
    modules: dict[str, ModuleInfo],
    rules: RulesConfig,
) -> tuple[list[str], set[str], bool]:
    """Split changed files into display output and directly impacted modules."""

    display_files: list[str] = []
    directly_affected: set[str] = set()
    saw_code_or_build_change = False

    for file_name in changed_files:
        path = PurePosixPath(file_name)
        display_files.append(file_name)

        if rules.skip_non_code_changes and is_doc_only_file(path, rules):
            continue

        saw_code_or_build_change = True
        module_name = module_for_path(path, modules)
        if module_name:
            directly_affected.add(module_name)
            continue

        if is_global_trigger_path(path, rules.global_trigger_paths):
            directly_affected.update(modules.keys())

    docs_only = bool(display_files) and rules.skip_non_code_changes and not saw_code_or_build_change
    return display_files, directly_affected, docs_only


def expand_dependents(start_modules: Iterable[str], reverse_deps: dict[str, set[str]]) -> set[str]:
    """Expand modules to include all transitive dependents."""

    affected: set[str] = set(start_modules)
    queue: deque[str] = deque(start_modules)

    while queue:
        module = queue.popleft()
        for dependent in reverse_deps.get(module, set()):
            if dependent not in affected:
                affected.add(dependent)
                queue.append(dependent)

    return affected


def is_doc_only_file(path: PurePosixPath, rules: RulesConfig) -> bool:
    """Return True if a path is documentation or non-code content."""

    name = path.name.lower()
    if name in rules.doc_file_names:
        return True

    return path.suffix.lower() in rules.doc_only_extensions


def module_for_path(path: PurePosixPath, modules: dict[str, ModuleInfo]) -> str | None:
    """Return the owning module for a changed path, preferring the deepest path."""

    matches = [
        module
        for module in modules.values()
        if _path_is_relative_to(path, module.relative_path)
    ]
    if not matches:
        return None

    matches.sort(key=lambda module: len(module.relative_path.parts), reverse=True)
    return matches[0].name


def is_global_trigger_path(path: PurePosixPath, trigger_paths: Iterable[str]) -> bool:
    """Return True when a changed path should force all modules to run."""

    for trigger in trigger_paths:
        normalized = trigger.strip()
        if not normalized:
            continue

        is_prefix = normalized.endswith("/")
        trigger_path = PurePosixPath(normalized.strip("/"))
        if is_prefix and _path_is_relative_to(path, trigger_path):
            return True
        if path == trigger_path:
            return True

    return False


def read_artifact_id(pom_path: Path) -> str:
    """Read the Maven artifactId from a pom file."""

    root = _parse_pom(pom_path)
    artifact_id = _child_text(root, "artifactId")
    if artifact_id:
        return artifact_id

    raise ImpactAnalysisError(f"Could not read artifactId from {pom_path}")


def module_selectors(modules: dict[str, ModuleInfo]) -> dict[str, str]:
    """Return Maven selectors keyed by module name."""

    return {name: module.selector for name, module in modules.items()}


def _modules_from_config(
    project_root: Path,
    configured_modules: tuple[ModuleConfig, ...],
) -> dict[str, ModuleInfo]:
    modules: dict[str, ModuleInfo] = {}
    for configured in configured_modules:
        relative_path = _normalize_module_path(configured.path)
        module_path = project_root / relative_path
        pom_path = module_path / "pom.xml"
        if not pom_path.is_file():
            raise ImpactAnalysisError(f"Configured module '{configured.name}' has no pom.xml at {pom_path}")

        artifact_id = configured.artifact_id or read_artifact_id(pom_path)
        modules[configured.name] = ModuleInfo(
            name=configured.name,
            path=module_path,
            relative_path=relative_path,
            artifact_id=artifact_id,
            selector=relative_path.as_posix(),
        )

    return modules


def _modules_from_paths(project_root: Path, module_paths: Iterable[str]) -> dict[str, ModuleInfo]:
    modules: dict[str, ModuleInfo] = {}
    for module_path_value in module_paths:
        relative_path = _normalize_module_path(module_path_value)
        module_path = project_root / relative_path
        pom_path = module_path / "pom.xml"
        if not pom_path.is_file():
            continue

        name = relative_path.name
        modules[name] = ModuleInfo(
            name=name,
            path=module_path,
            relative_path=relative_path,
            artifact_id=read_artifact_id(pom_path),
            selector=relative_path.as_posix(),
        )

    return modules


def _read_declared_modules(root_pom: Path) -> list[str]:
    root = _parse_pom(root_pom)
    return [
        module.text.strip()
        for module in _find_modules(root)
        if module.text and module.text.strip()
    ]


def _parse_pom(pom_path: Path) -> ET.Element:
    try:
        return ET.parse(pom_path).getroot()
    except ET.ParseError as exc:
        raise ImpactAnalysisError(f"Could not parse Maven pom {pom_path}: {exc}") from exc
    except OSError as exc:
        raise ImpactAnalysisError(f"Could not read Maven pom {pom_path}: {exc}") from exc


def _find_modules(root: ET.Element) -> list[ET.Element]:
    namespace = _namespace(root)
    if namespace:
        return root.findall("m:modules/m:module", namespace)
    return root.findall("modules/module")


def _find_dependencies(root: ET.Element) -> list[ET.Element]:
    namespace = _namespace(root)
    if namespace:
        return root.findall(".//m:dependencies/m:dependency", namespace)
    return root.findall(".//dependencies/dependency")


def _child_text(element: ET.Element, child_name: str) -> str:
    namespace_uri = _namespace_uri(element)
    child = element.find(f"{{{namespace_uri}}}{child_name}" if namespace_uri else child_name)
    return child.text.strip() if child is not None and child.text else ""


def _namespace(root: ET.Element) -> dict[str, str]:
    namespace_uri = _namespace_uri(root)
    return {"m": namespace_uri} if namespace_uri else {}


def _namespace_uri(element: ET.Element) -> str:
    if element.tag.startswith("{") and "}" in element.tag:
        return element.tag.split("}", 1)[0].strip("{")
    return ""


def _normalize_module_path(value: str) -> PurePosixPath:
    normalized = value.strip().strip("/")
    return PurePosixPath(normalized)


def _path_is_relative_to(path: PurePosixPath, parent: PurePosixPath) -> bool:
    if not parent.parts:
        return False
    return path == parent or path.parts[: len(parent.parts)] == parent.parts


def _shared_module_changed(
    changed_files: Iterable[str],
    modules: dict[str, ModuleInfo],
    shared_modules: Iterable[str],
    rules: RulesConfig,
) -> bool:
    shared = set(shared_modules)
    if not shared:
        return False

    for file_name in changed_files:
        path = PurePosixPath(file_name)
        if rules.skip_non_code_changes and is_doc_only_file(path, rules):
            continue

        module_name = module_for_path(path, modules)
        if module_name in shared:
            return True

    return False

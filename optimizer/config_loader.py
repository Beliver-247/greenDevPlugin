"""Configuration loading for the selective build optimizer."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_DOC_ONLY_EXTENSIONS = frozenset(
    {
        ".adoc",
        ".asciidoc",
        ".drawio",
        ".gif",
        ".jpeg",
        ".jpg",
        ".markdown",
        ".md",
        ".pdf",
        ".png",
        ".rst",
        ".svg",
        ".txt",
    }
)
DEFAULT_DOC_FILE_NAMES = frozenset({"changelog", "license", "readme", "readme.md"})
DEFAULT_GLOBAL_TRIGGER_PATHS = ("pom.xml", ".mvn/", "mvnw", "mvnw.cmd", "settings.xml")


class ConfigurationError(RuntimeError):
    """Raised when the optimizer configuration is invalid."""


@dataclass(frozen=True)
class ModuleConfig:
    """Configured Maven module metadata."""

    name: str
    path: str
    artifact_id: str | None = None


@dataclass(frozen=True)
class MavenConfig:
    """Maven execution settings."""

    executable: str = "mvn"
    group_id: str | None = None
    also_make: bool = True
    also_make_tests: bool = False
    extra_args: tuple[str, ...] = ()
    build_goals: tuple[str, ...] = ("clean", "install")
    test_goals: tuple[str, ...] = ("test",)
    run_build: bool = True
    run_tests: bool = True


@dataclass(frozen=True)
class RulesConfig:
    """Rules that control change classification and impact expansion."""

    skip_non_code_changes: bool = True
    doc_only_extensions: frozenset[str] = DEFAULT_DOC_ONLY_EXTENSIONS
    doc_file_names: frozenset[str] = DEFAULT_DOC_FILE_NAMES
    global_trigger_paths: tuple[str, ...] = DEFAULT_GLOBAL_TRIGGER_PATHS


@dataclass(frozen=True)
class OutputConfig:
    """Structured output settings."""

    format: str = "json"


@dataclass(frozen=True)
class OptimizerConfig:
    """Complete runtime configuration for the optimizer."""

    project_root: Path
    base_ref: str = "HEAD~1"
    head_ref: str = "HEAD"
    dry_run: bool = False
    modules: tuple[ModuleConfig, ...] = ()
    shared_modules: frozenset[str] = frozenset()
    rules: RulesConfig = RulesConfig()
    maven: MavenConfig = MavenConfig()
    output: OutputConfig = OutputConfig()


def load_config(config_path: Path | None, project_root: Path) -> OptimizerConfig:
    """Load optimizer configuration from YAML or JSON, falling back to defaults."""

    config = OptimizerConfig(project_root=project_root)
    if config_path is None:
        return config

    if not config_path.exists():
        return config

    data = _load_mapping(config_path)
    return _merge_config(config, data)


def apply_cli_overrides(
    config: OptimizerConfig,
    *,
    base_ref: str | None = None,
    head_ref: str | None = None,
    dry_run: bool | None = None,
    output_format: str | None = None,
) -> OptimizerConfig:
    """Apply command-line overrides to a loaded configuration."""

    output = config.output
    if output_format is not None:
        output = replace(output, format=output_format)

    return replace(
        config,
        base_ref=base_ref or config.base_ref,
        head_ref=head_ref or config.head_ref,
        dry_run=config.dry_run if dry_run is None else dry_run,
        output=output,
    )


def default_config_path(project_root: Path) -> Path | None:
    """Return the conventional default config path when present."""

    candidate = project_root / "config" / "default.yaml"
    return candidate if candidate.exists() else None


def resolve_config_path(config_arg: str | None, project_root: Path) -> Path | None:
    """Resolve a user-supplied config path without assuming a local checkout layout."""

    if not config_arg:
        return default_config_path(project_root)

    candidate = Path(config_arg).expanduser()
    if candidate.is_absolute():
        return candidate

    cwd_candidate = Path.cwd() / candidate
    if cwd_candidate.exists():
        return cwd_candidate

    return project_root / candidate


def _load_mapping(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"Could not read config file {path}: {exc}") from exc

    try:
        if suffix == ".json":
            loaded = json.loads(text)
        elif suffix in {".yaml", ".yml"}:
            loaded = _load_yaml(text)
        else:
            raise ConfigurationError(
                f"Unsupported config file extension '{path.suffix}'. Use YAML or JSON."
            )
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError(f"Could not parse config file {path}: {exc}") from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigurationError(f"Config file {path} must contain a mapping at the top level.")

    return loaded


def _load_yaml(text: str) -> Any:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return _parse_simple_yaml(text)

    return yaml.safe_load(text)


def _merge_config(default: OptimizerConfig, data: Mapping[str, Any]) -> OptimizerConfig:
    git_data = _as_mapping(data.get("git"), "git")
    rules_data = _as_mapping(data.get("rules"), "rules")
    maven_data = _as_mapping(data.get("maven"), "maven")
    output_data = _as_mapping(data.get("output"), "output")

    modules = _parse_modules(data.get("modules", default.modules))
    rules = _parse_rules(default.rules, rules_data)
    maven = _parse_maven(default.maven, maven_data)
    output = _parse_output(default.output, output_data)

    return replace(
        default,
        base_ref=_as_str(git_data.get("base_ref", data.get("base_ref", default.base_ref))),
        head_ref=_as_str(git_data.get("head_ref", data.get("head_ref", default.head_ref))),
        dry_run=_as_bool(data.get("dry_run", default.dry_run), "dry_run"),
        modules=modules,
        shared_modules=frozenset(_as_str_list(data.get("shared_modules", default.shared_modules))),
        rules=rules,
        maven=maven,
        output=output,
    )


def _parse_modules(value: Any) -> tuple[ModuleConfig, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ConfigurationError("'modules' must be a list.")

    modules: list[ModuleConfig] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ConfigurationError(f"modules[{index}] must be a mapping.")

        name = item.get("name")
        path = item.get("path", name)
        if not name or not path:
            raise ConfigurationError(f"modules[{index}] must include 'name' and 'path'.")

        artifact_id = item.get("artifact_id")
        modules.append(
            ModuleConfig(
                name=_as_str(name),
                path=_as_str(path),
                artifact_id=None if artifact_id in (None, "") else _as_str(artifact_id),
            )
        )

    return tuple(modules)


def _parse_rules(default: RulesConfig, data: Mapping[str, Any]) -> RulesConfig:
    return RulesConfig(
        skip_non_code_changes=_as_bool(
            data.get("skip_non_code_changes", default.skip_non_code_changes),
            "rules.skip_non_code_changes",
        ),
        doc_only_extensions=frozenset(
            _normalize_extension(value)
            for value in _as_str_list(data.get("doc_only_extensions", default.doc_only_extensions))
        ),
        doc_file_names=frozenset(
            value.lower()
            for value in _as_str_list(data.get("doc_file_names", default.doc_file_names))
        ),
        global_trigger_paths=tuple(
            _as_str_list(data.get("global_trigger_paths", default.global_trigger_paths))
        ),
    )


def _parse_maven(default: MavenConfig, data: Mapping[str, Any]) -> MavenConfig:
    group_id = data.get("group_id", default.group_id)
    return MavenConfig(
        executable=_as_str(data.get("executable", default.executable)),
        group_id=None if group_id in (None, "") else _as_str(group_id),
        also_make=_as_bool(data.get("also_make", default.also_make), "maven.also_make"),
        also_make_tests=_as_bool(data.get("also_make_tests", default.also_make_tests), "maven.also_make_tests"),
        extra_args=tuple(_as_str_list(data.get("extra_args", default.extra_args))),
        build_goals=tuple(_as_str_list(data.get("build_goals", default.build_goals))),
        test_goals=tuple(_as_str_list(data.get("test_goals", default.test_goals))),
        run_build=_as_bool(data.get("run_build", default.run_build), "maven.run_build"),
        run_tests=_as_bool(data.get("run_tests", default.run_tests), "maven.run_tests"),
    )


def _parse_output(default: OutputConfig, data: Mapping[str, Any]) -> OutputConfig:
    output_format = _as_str(data.get("format", default.format)).lower()
    if output_format not in {"json", "key-value", "none"}:
        raise ConfigurationError("output.format must be one of: json, key-value, none.")

    return OutputConfig(format=output_format)


def _as_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"'{name}' must be a mapping.")
    return value


def _as_str(value: Any) -> str:
    return str(value).strip()


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False

    raise ConfigurationError(f"'{name}' must be true or false.")


def _as_str_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (set, frozenset, tuple, list)):
        return [_as_str(item) for item in value]
    if isinstance(value, str):
        return [value]

    raise ConfigurationError(f"Expected a list of strings, got {type(value).__name__}.")


def _normalize_extension(value: str) -> str:
    normalized = value.strip().lower()
    return normalized if normalized.startswith(".") else f".{normalized}"


def _parse_simple_yaml(text: str) -> Any:
    """Parse the small YAML subset used by the bundled example config.

    PyYAML is used when installed. This fallback keeps the optimizer runnable in
    minimal CI containers while still supporting dictionaries, lists, booleans,
    nulls, quoted strings, and inline scalar lists.
    """

    lines: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        content = _strip_comment(raw_line.strip())
        if content:
            lines.append((indent, content))

    if not lines:
        return {}

    value, index = _parse_yaml_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise ConfigurationError("Unsupported YAML structure.")
    return value


def _parse_yaml_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return {}, index

    current_indent, content = lines[index]
    if current_indent < indent:
        return {}, index
    if current_indent > indent:
        indent = current_indent

    if content.startswith("- "):
        return _parse_yaml_list(lines, index, indent)
    return _parse_yaml_mapping(lines, index, indent)


def _parse_yaml_mapping(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[dict[str, Any], int]:
    mapping: dict[str, Any] = {}

    while index < len(lines):
        current_indent, content = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise ConfigurationError(f"Unexpected indentation near '{content}'.")
        if content.startswith("- "):
            break

        key, raw_value = _split_yaml_key_value(content)
        if raw_value == "":
            if index + 1 >= len(lines) or lines[index + 1][0] <= current_indent:
                mapping[key] = None
                index += 1
            else:
                value, index = _parse_yaml_block(lines, index + 1, lines[index + 1][0])
                mapping[key] = value
        else:
            mapping[key] = _parse_yaml_scalar(raw_value)
            index += 1

    return mapping, index


def _parse_yaml_list(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[list[Any], int]:
    items: list[Any] = []

    while index < len(lines):
        current_indent, content = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise ConfigurationError(f"Unexpected indentation near '{content}'.")
        if not content.startswith("- "):
            break

        item_text = content[2:].strip()
        if item_text == "":
            value, index = _parse_yaml_block(lines, index + 1, indent + 2)
            items.append(value)
            continue

        if _looks_like_key_value(item_text):
            key, raw_value = _split_yaml_key_value(item_text)
            item: dict[str, Any] = {}
            index += 1
            if raw_value == "":
                if index < len(lines) and lines[index][0] > current_indent:
                    nested_value, index = _parse_yaml_block(lines, index, lines[index][0])
                    item[key] = nested_value
                else:
                    item[key] = None
            else:
                item[key] = _parse_yaml_scalar(raw_value)

            while index < len(lines):
                next_indent, next_content = lines[index]
                if next_indent <= current_indent:
                    break
                if next_indent != current_indent + 2 or next_content.startswith("- "):
                    break

                child_key, child_raw_value = _split_yaml_key_value(next_content)
                if child_raw_value == "":
                    if index + 1 < len(lines) and lines[index + 1][0] > next_indent:
                        nested_value, index = _parse_yaml_block(lines, index + 1, lines[index + 1][0])
                        item[child_key] = nested_value
                    else:
                        item[child_key] = None
                        index += 1
                else:
                    item[child_key] = _parse_yaml_scalar(child_raw_value)
                    index += 1

            items.append(item)
        else:
            items.append(_parse_yaml_scalar(item_text))
            index += 1

    return items, index


def _split_yaml_key_value(content: str) -> tuple[str, str]:
    if ":" not in content:
        raise ConfigurationError(f"Expected 'key: value' near '{content}'.")
    key, value = content.split(":", 1)
    key = key.strip()
    if not key:
        raise ConfigurationError(f"Missing key near '{content}'.")
    return key, value.strip()


def _looks_like_key_value(value: str) -> bool:
    return ":" in value and not value.startswith(("'", '"'))


def _parse_yaml_scalar(value: str) -> Any:
    normalized = value.strip()
    lowered = normalized.lower()

    if lowered in {"null", "none", "~"}:
        return None
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if normalized == "[]":
        return []
    if normalized == "{}":
        return {}
    if normalized.startswith("[") and normalized.endswith("]"):
        inner = normalized[1:-1].strip()
        if not inner:
            return []
        return [_parse_yaml_scalar(part.strip()) for part in inner.split(",")]
    if (
        len(normalized) >= 2
        and normalized[0] == normalized[-1]
        and normalized.startswith(("'", '"'))
    ):
        return normalized[1:-1]

    return normalized


def _strip_comment(value: str) -> str:
    quote: str | None = None
    for index, char in enumerate(value):
        if char in {"'", '"'}:
            quote = None if quote == char else char
        if char == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()

    return value

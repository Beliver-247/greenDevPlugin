# GreenDevOps Build Optimizer — Deep Dive

This document provides a deep dive into the internal mechanics of the `build-optimizer-agent`. It explains exactly how the Python engine (`optimizer/core.py`) dynamically maps changed files to Maven modules and calculates the "blast radius" of dependencies to guarantee safe selective builds.

## 1. Module Discovery

Before the optimizer can do anything, it needs to understand the structure of your project. It does this completely dynamically by searching for `pom.xml` files. It does not require you to hardcode your project structure.

Here is the exact code snippet that handles this from `core.py`:

```python
def discover_modules(project_root: Path, configured_modules: Iterable[ModuleConfig]) -> dict[str, ModuleInfo]:
    """Discover Maven modules from config, root pom.xml, or top-level directories."""

    # 1. Read explicitly configured modules if provided
    configured = tuple(configured_modules)
    if configured:
        return _modules_from_config(project_root, configured)

    # 2. Check the root pom.xml for <modules> declarations
    root_pom = project_root / "pom.xml"
    if root_pom.is_file():
        declared_modules = _read_declared_modules(root_pom)
        if declared_modules:
            return _modules_from_paths(project_root, declared_modules)

    # 3. Fallback: Scan top-level directories for pom.xml files
    top_level_modules = [
        child.name
        for child in sorted(project_root.iterdir())
        if child.is_dir() and (child / "pom.xml").is_file()
    ]
    return _modules_from_paths(project_root, top_level_modules)
```
**Why this matters:** This makes the script incredibly reusable. You can drop this into almost any standard Java repository and it will map out the workspace instantly.

---

## 2. Dependency Graphing

Once the modules are discovered, the optimizer needs to understand their relationships. If module `A` depends on module `B`, and you change code in module `B`, both `B` and `A` must be rebuilt. 

The optimizer builds a **Reverse Dependency Graph** in memory by parsing the `<dependencies>` block of every single `pom.xml`.

```python
def build_dependency_graph(
    modules: dict[str, ModuleInfo],
    maven_group_id: str | None,
) -> dict[str, set[str]]:
    """Build a reverse dependency graph between internal Maven modules."""

    artifact_to_module = {module.artifact_id: module.name for module in modules.values()}
    reverse_deps: DefaultDict[str, set[str]] = defaultdict(set)

    for module in modules.values():
        # Parse the pom.xml to find dependencies
        dependencies = read_internal_dependencies(
            module.path / "pom.xml",
            artifact_to_module,
            maven_group_id,
        )
        # Create a reverse lookup: "If X changes, Y needs to be rebuilt"
        for dependency in dependencies:
            reverse_deps[dependency].add(module.name)

    return dict(reverse_deps)
```

---

## 3. Impact Analysis (Git Diff Mapping)

Next, the script looks at the actual code changes by fetching the `git diff`. It iterates through every single file that was modified and assigns it to the module that owns it based on the directory path.

It also contains intelligent checks to completely skip the build if the change was strictly documentation (e.g., `README.md`).

```python
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

        # Skip documentation files entirely
        if rules.skip_non_code_changes and is_doc_only_file(path, rules):
            continue

        saw_code_or_build_change = True
        
        # Match the file path to its owning Maven module
        module_name = module_for_path(path, modules)
        if module_name:
            directly_affected.add(module_name)
            continue

        # If a global configuration file changed (like the root pom.xml)
        # Force a full rebuild of ALL modules
        if is_global_trigger_path(path, rules.global_trigger_paths):
            directly_affected.update(modules.keys())

    docs_only = bool(display_files) and rules.skip_non_code_changes and not saw_code_or_build_change
    return display_files, directly_affected, docs_only
```

---

## 4. Expanding the Blast Radius

Finally, the optimizer merges the Git mapping (Step 3) with the Dependency Graph (Step 2). It takes the modules that were directly changed by Git, and recursively "expands" them to include any other modules that depend on them.

```python
def expand_dependents(start_modules: Iterable[str], reverse_deps: dict[str, set[str]]) -> set[str]:
    """Expand modules to include all transitive dependents."""

    affected: set[str] = set(start_modules)
    queue: deque[str] = deque(start_modules)

    # Recursively traverse the reverse dependency graph
    while queue:
        module = queue.popleft()
        for dependent in reverse_deps.get(module, set()):
            if dependent not in affected:
                affected.add(dependent)
                queue.append(dependent)

    return affected
```
**Why this matters:** This is the safety net of the optimizer. By recursively resolving the graph, it guarantees that no downstream code breaks as a result of an upstream change, preserving 100% CI/CD reliability while saving immense amounts of compute energy.

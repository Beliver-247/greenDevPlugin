# CI/CD Optimizer

Reusable Python plugin for selective Maven builds in multi-module Spring Boot repositories.

The optimizer compares two Git refs, maps changed files to Maven modules, expands the impact through internal module dependencies, and runs only the Maven build/test commands that are needed.

## Project Structure

```text
project-root/
+-- optimizer/
|   +-- __init__.py
|   +-- __main__.py
|   +-- cli.py
|   +-- config_loader.py
|   +-- core.py
|   +-- git_utils.py
|   +-- maven_utils.py
|   +-- scheduler/
|       +-- __init__.py
|       +-- carbon_api.py
|       +-- features.py
|       +-- decision_engine.py
|       +-- predictor.py
|       +-- scheduler.py
+-- config/
|   +-- default.yaml
+-- Dockerfile
+-- .gitignore
+-- README.md
+-- selective_build.py
```

`selective_build.py` is kept as a backward-compatible wrapper. New usage should call `python -m optimizer`.

## Usage

```bash
python -m optimizer --base origin/main --head HEAD
python -m optimizer --base origin/main --head HEAD --dry-run
python -m optimizer --base origin/main --head HEAD --dry-run false
python -m optimizer --config config/default.yaml --output-format json
```

Useful options:

```text
--base <git_base_ref>       Git ref to compare from
--head <git_head_ref>       Git ref to compare to
--dry-run [true|false]      Print Maven actions without executing them
--config <path>             YAML or JSON config path
--project-root <path>       Repository root, defaults to current directory
--output-format <format>    json, key-value, or none
--carbon-aware              Run carbon-aware scheduling after the build
```

## Configuration

If no config file is provided, the optimizer uses `config/default.yaml` when present, otherwise built-in defaults.

```yaml
git:
  base_ref: origin/main
  head_ref: HEAD

modules:
  - name: auth-service
    path: auth-service/
  - name: billing-service
    path: services/billing-service/

shared_modules:
  - common-lib

rules:
  skip_non_code_changes: true
  doc_only_extensions:
    - .md
    - .txt
  doc_file_names:
    - readme.md
    - changelog
  global_trigger_paths:
    - pom.xml
    - .mvn/
    - mvnw
    - mvnw.cmd
    - settings.xml

maven:
  executable: mvn
  group_id: com.example
  also_make: true
  also_make_tests: false
  extra_args: []
  build_goals:
    - clean
    - install
  test_goals:
    - test
  run_build: true
  run_tests: true

output:
  format: json
```

Leave `modules: []` to auto-discover modules from the root `pom.xml` or top-level directories containing `pom.xml`.

## Exit Codes

```text
0   success
10  no changes
20  documentation-only changes
30  no affected modules
1   configuration, Git, impact-analysis, or Maven failure
```

## Structured Output

The CLI always prints readable logs and, by default, a final JSON object:

```json
{
  "status": "success",
  "exit_code": 0,
  "elapsed_seconds": 1.23,
  "base_ref": "origin/main",
  "head_ref": "HEAD",
  "config_path": "config/default.yaml",
  "changed_files": [
    "auth-service/src/main/java/App.java"
  ],
  "directly_affected_modules": [
    "auth-service"
  ],
  "affected_modules": [
    "auth-service"
  ],
  "actions": [
    {
      "name": "build",
      "command": [
        "mvn",
        "-pl",
        "auth-service",
        "-am",
        "-DskipTests",
        "clean",
        "install"
      ],
      "dry_run": false
    },
    {
      "name": "test",
      "command": [
        "mvn",
        "-pl",
        "auth-service",
        "test"
      ],
      "dry_run": false
    }
  ]
}
```

Use `--output-format key-value` for CI systems that prefer shell-friendly values.

## Carbon-Aware Scheduling

Pass `--carbon-aware` to append a scheduling recommendation to the output. The optimizer runs normally and then consults the carbon-aware scheduler to determine whether the build should execute immediately or be delayed to a lower-intensity window.

```bash
python -m optimizer \
  --base origin/main \
  --head HEAD \
  --dry-run \
  --carbon-aware
```

When carbon intensity is high and a significantly greener window is forecast, the output includes:

```json
{
  "status": "success",
  "affected_modules": ["auth-service"],
  "actions": ["..."],
  "scheduling": {
    "action": "schedule",
    "scheduled_hour": 3,
    "target_intensity": 180.0
  }
}
```

When the grid is already clean, or no meaningful improvement is forecast:

```json
{
  "scheduling": {
    "action": "execute_now"
  }
}
```

Without `--carbon-aware`, no `scheduling` key appears and all behaviour is unchanged.

### Future Integration Points

| Area | Description |
|---|---|
| **ElectricityMap API** | Replace `MockCarbonDataProvider` with a real provider that fetches live marginal carbon intensity from ElectricityMap. |
| **WattTime API** | Alternative real-time grid data source with MOER (Marginal Operating Emissions Rate) signals. |
| **XGBoost predictor** | Train a model on historical carbon + build data and swap in `GreenWindowPredictor.predict()` for smarter scheduling. |
| **Carbon-aware policies** | Add configurable policies (e.g., max delay tolerance, business-hour constraints) to the decision engine. |

## GitHub Actions

```yaml
name: Selective Maven Build

on:
  pull_request:
  push:
    branches: [main]

jobs:
  selective-build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-java@v4
        with:
          distribution: temurin
          java-version: '17'

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Run optimizer
        run: |
          python -m optimizer \
            --base origin/main \
            --head HEAD \
            --config config/default.yaml
```

## Jenkins

```groovy
pipeline {
  agent any

  stages {
    stage('Selective Maven Build') {
      steps {
        checkout scm
        sh '''
          python -m optimizer \
            --base origin/main \
            --head HEAD \
            --config config/default.yaml \
            --output-format key-value
        '''
      }
    }
  }
}
```

## Docker

Build the optimizer image:

```bash
docker build -t ci-cd-optimizer .
```

Run it against a checked-out repository:

```bash
docker run --rm -v "$PWD:/workspace" ci-cd-optimizer --base origin/main --head HEAD
```

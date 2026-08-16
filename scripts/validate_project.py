"""Static validation with no third-party dependencies.

Complements pytest rather than duplicating it: pytest verifies behaviour once
the code imports, this catches the defects unit tests here historically missed.

* an element ID renamed in HTML but not in the JS that calls
  ``getElementById`` on it (this exact bug shipped to GitHub Pages once);
* a module that no longer parses, or imports a name a sibling does not define;
* ``CodeUri`` pointing at a directory that would package documents, tests or
  local state into Lambda;
* documentation asserting a security control the template does not implement;
* fixtures whose feature list has drifted from ScoreFactors.

    python scripts/validate_project.py
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
APP = SRC / "app"

FEATURE_NAMES = [
    "availability",
    "capacity",
    "skill_match",
    "region_match",
    "completion_rate",
    "similar_job_rate",
    "rework_history",
    "sla_fit",
    "risk_fit",
]

failures: list[str] = []
checks_run = 0


def check(condition: bool, message: str) -> None:
    global checks_run
    checks_run += 1
    if not condition:
        failures.append(message)


def section(title: str) -> None:
    print(f"\n== {title} ==")


# --------------------------------------------------------------------------
# 1. Python: every module parses, and intra-package imports resolve.
# --------------------------------------------------------------------------
def validate_python() -> None:
    section("Python syntax and intra-package imports")
    modules = sorted(SRC.rglob("*.py")) + sorted((PROJECT_ROOT / "tests").rglob("*.py"))
    modules += sorted((PROJECT_ROOT / "scripts").rglob("*.py"))

    app_modules = {path.stem for path in APP.glob("*.py")}
    parsed: dict[Path, ast.Module] = {}

    for path in modules:
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            parsed[path] = tree
        except SyntaxError as error:
            failures.append(f"SyntaxError {path.relative_to(PROJECT_ROOT)}: {error}")
        checks_run_local = True
        assert checks_run_local

    print(f"  parsed {len(parsed)} python files")

    # Top-level names each app module defines, so imports can be checked at the
    # symbol level rather than only the module level. Without this, renaming a
    # function and missing one caller is invisible until runtime -- and pytest
    # only catches it if a test happens to import that path.
    exported: dict[str, set[str]] = {}
    for module_path in sorted(APP.glob("*.py")):
        tree = parsed.get(module_path)
        if tree is None:
            continue
        names: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                # Re-exported names count: `from .models import X` makes X
                # importable from that module too.
                names.update(
                    alias.asname or alias.name.split(".")[0] for alias in node.names
                )
        exported[module_path.stem] = names

    def check_import(path: Path, node: ast.ImportFrom, target: str) -> None:
        location = f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}"
        if target not in app_modules:
            failures.append(
                f"{location} imports '{node.module}', which is not a module in "
                f"src/app/"
            )
            return
        for alias in node.names:
            if alias.name == "*":
                continue
            check(
                alias.name in exported.get(target, set()),
                f"{location} imports '{alias.name}' from app.{target}, "
                f"which does not define it",
            )

    for path, tree in parsed.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            # Relative import inside the app package: `from .scoring import x`
            if node.level == 1 and node.module and APP in path.parents:
                check_import(path, node, node.module)
            # Absolute import from handlers and tests: `from app.scoring import x`
            elif (node.module or "").startswith("app."):
                check_import(path, node, node.module.split(".", 1)[1].split(".")[0])

    # Names the handlers reference must be defined where the template expects.
    for handler_file, handler_name in (
        ("lambda_function.py", "handler"),
        ("lambda_http.py", "handler"),
    ):
        path = SRC / handler_file
        check(path.exists(), f"Missing handler module src/{handler_file}")
        if not path.exists():
            continue
        tree = parsed.get(path)
        if tree is None:
            continue
        defined = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        } | {
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        check(
            handler_name in defined,
            f"src/{handler_file} does not define '{handler_name}'",
        )


# --------------------------------------------------------------------------
# 2. Dashboards: every getElementById target exists in the matching HTML.
# --------------------------------------------------------------------------
ID_PATTERNS = [
    re.compile(r"""\$\(\s*["']([a-zA-Z0-9_-]+)["']\s*\)"""),
    re.compile(r"""getElementById\(\s*["']([a-zA-Z0-9_-]+)["']\s*\)"""),
]


def html_ids(html: str) -> set[str]:
    return set(re.findall(r"""\bid\s*=\s*["']([^"']+)["']""", html))


def validate_dashboard(name: str, html_path: Path, js_paths: list[Path]) -> None:
    section(f"{name} dashboard element bindings")
    if not html_path.exists():
        failures.append(f"Missing {html_path.relative_to(PROJECT_ROOT)}")
        return
    available = html_ids(html_path.read_text(encoding="utf-8"))
    referenced: dict[str, Path] = {}
    for js_path in js_paths:
        if not js_path.exists():
            failures.append(f"Missing {js_path.relative_to(PROJECT_ROOT)}")
            continue
        source = js_path.read_text(encoding="utf-8")
        for pattern in ID_PATTERNS:
            for element_id in pattern.findall(source):
                referenced.setdefault(element_id, js_path)

    for element_id, js_path in sorted(referenced.items()):
        check(
            element_id in available,
            f"{js_path.relative_to(PROJECT_ROOT)} uses #{element_id}, "
            f"which does not exist in {html_path.relative_to(PROJECT_ROOT)}",
        )
    print(f"  {len(referenced)} referenced ids, {len(available)} defined in HTML")

    # Every <form> the JS submits must have a submit listener, otherwise the
    # `required` attributes on its inputs are never enforced.
    html = html_path.read_text(encoding="utf-8")
    for form_id in re.findall(r"""<form[^>]*\bid\s*=\s*["']([^"']+)["']""", html):
        listens = any(
            re.search(
                rf"""\$\(["']{re.escape(form_id)}["']\)\.addEventListener\(\s*["']submit["']""",
                path.read_text(encoding="utf-8"),
            )
            for path in js_paths
            if path.exists()
        )
        check(
            listens,
            f"<form id={form_id}> in {html_path.relative_to(PROJECT_ROOT)} has no "
            f"submit listener, so its required attributes are never enforced",
        )


def validate_config_global() -> None:
    section("Frontend config global consistency")
    names: dict[str, list[str]] = {}
    targets = [
        PROJECT_ROOT / "frontend" / "config.js",
        PROJECT_ROOT / "frontend" / "app.js",
        PROJECT_ROOT / "frontend" / "aws-client.js",
        PROJECT_ROOT / ".github" / "workflows" / "deploy.yml",
    ]
    for path in targets:
        if not path.exists():
            failures.append(f"Missing {path.relative_to(PROJECT_ROOT)}")
            continue
        for match in re.findall(
            r"window\.([A-Z0-9_]+_CONFIG)", path.read_text(encoding="utf-8")
        ):
            names.setdefault(match, []).append(str(path.relative_to(PROJECT_ROOT)))
    check(
        len(names) == 1,
        f"Frontend config global name is inconsistent across files: {names}",
    )
    for name, where in names.items():
        check(
            len(where) >= 4,
            f"{name} is referenced in only {len(where)} of 4 expected files: {where}",
        )
        print(f"  {name} referenced in {len(where)} files")


# --------------------------------------------------------------------------
# 3. Packaging: CodeUri must not ship documents, tests, or local state.
# --------------------------------------------------------------------------
FORBIDDEN_IN_PACKAGE = {
    "tests",
    "docs",
    "frontend",
    "scripts",
    ".github",
    "runtime",
    "answers.md",
    "README.md",
    "audit.log",
    ".env",
}


def validate_packaging() -> None:
    section("Lambda packaging boundary")
    template = (PROJECT_ROOT / "template.yaml").read_text(encoding="utf-8")
    code_uris = re.findall(r"^\s*CodeUri:\s*(\S+)\s*$", template, flags=re.MULTILINE)
    check(bool(code_uris), "template.yaml declares no CodeUri")
    for uri in code_uris:
        check(
            uri not in {".", "./"},
            "template.yaml uses `CodeUri: .`, which packages the whole "
            "repository into Lambda (SAM CLI has no .samignore)",
        )
        package_dir = (PROJECT_ROOT / uri).resolve()
        check(package_dir.is_dir(), f"CodeUri {uri} is not a directory")
        if not package_dir.is_dir():
            continue
        shipped = {entry.name for entry in package_dir.iterdir()}
        leaked = sorted(shipped & FORBIDDEN_IN_PACKAGE)
        check(not leaked, f"CodeUri {uri} would package: {leaked}")
        check(
            (package_dir / "requirements.txt").exists(),
            f"CodeUri {uri} has no requirements.txt; SAM would ship no dependencies",
        )
        print(f"  CodeUri {uri} -> {sorted(shipped)}")

    # A .samignore would be a no-op and imply protection that does not exist.
    check(
        not (PROJECT_ROOT / ".samignore").exists(),
        ".samignore exists but SAM CLI does not support it; scope CodeUri instead",
    )

    # Any role that reads objects must also be able to list the bucket.
    # Without s3:ListBucket, S3 answers GetObject on a *missing* key with 403
    # AccessDenied rather than 404 NoSuchKey, so a store that reads a key
    # before writing it fails on first use only. That shipped once, as
    # "Final decision state could not be loaded." on the first decision.
    get_object_grants = template.count("s3:GetObject")
    list_bucket_grants = template.count("s3:ListBucket")
    check(
        list_bucket_grants >= get_object_grants,
        f"template.yaml grants s3:GetObject {get_object_grants} time(s) but "
        f"s3:ListBucket only {list_bucket_grants}. Without ListBucket on the "
        f"bucket ARN, a read of a not-yet-written key returns AccessDenied "
        f"instead of NoSuchKey",
    )
    print(f"  s3:GetObject x{get_object_grants}, s3:ListBucket x{list_bucket_grants}")


# --------------------------------------------------------------------------
# 4. Documentation must not claim controls the template does not implement.
# --------------------------------------------------------------------------
def validate_docs() -> None:
    section("Documentation claims vs template")
    template = (PROJECT_ROOT / "template.yaml").read_text(encoding="utf-8")
    uses_cognito_jwt = "CognitoAuthorizer" in template and "JwtConfiguration" in template
    check(uses_cognito_jwt, "template.yaml no longer configures a Cognito JWT authorizer")

    banned = re.compile(r"(IAM/SigV4|SigV4-signed|IAM authorizer)", re.IGNORECASE)
    for path in sorted(PROJECT_ROOT.rglob("*.md")):
        if any(
            part in path.parts
            for part in (".venv", ".aws-sam", "node_modules", "__pycache__")
        ):
            continue
        if path.name in {"CODE_REVIEW.md", "Requirement_testproject.md"}:
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if banned.search(line) and "no longer" not in line and "was " not in line:
                failures.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{number} claims IAM/SigV4 "
                    f"authorization, but the API uses a Cognito JWT authorizer"
                )
            checks_run_local = True
            assert checks_run_local

    # /api/infrastructure is served to the dashboard, so it counts as a doc.
    main_source = (APP / "main.py").read_text(encoding="utf-8")
    check(
        "IAM/SigV4" not in main_source,
        "app/main.py still advertises IAM/SigV4 authorization to the dashboard",
    )
    print("  scanned markdown and /api/infrastructure copy")


# --------------------------------------------------------------------------
# 5. Fixtures agree with the ScoreFactors contract.
# --------------------------------------------------------------------------
def validate_fixtures() -> None:
    section("Data fixtures")
    models = (APP / "models.py").read_text(encoding="utf-8")
    tree = ast.parse(models)
    score_factor_fields: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ScoreFactors":
            score_factor_fields = [
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            ]
    check(
        score_factor_fields == FEATURE_NAMES,
        f"ScoreFactors fields {score_factor_fields} differ from the expected "
        f"feature order {FEATURE_NAMES}",
    )

    for relative in ("data/training.json", "frontend/training.json"):
        path = PROJECT_ROOT / relative
        if not path.exists():
            failures.append(f"Missing {relative}")
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        check(
            payload.get("feature_names") == FEATURE_NAMES,
            f"{relative} feature_names do not match ScoreFactors",
        )
        examples = payload.get("examples", [])
        check(
            len(examples) >= len(FEATURE_NAMES),
            f"{relative} has {len(examples)} examples for "
            f"{len(FEATURE_NAMES)} features; the fit would be unconstrained",
        )
        bad_width = [
            index
            for index, item in enumerate(examples)
            if len(item.get("factors", [])) != len(FEATURE_NAMES)
        ]
        check(not bad_width, f"{relative} rows with wrong width: {bad_width[:5]}")
        out_of_range = [
            index
            for index, item in enumerate(examples)
            if not all(0.0 <= value <= 1.0 for value in item["factors"])
            or not 0.0 <= item["outcome"] <= 1.0
        ]
        check(
            not out_of_range,
            f"{relative} rows outside [0,1]: {out_of_range[:5]}",
        )
        print(f"  {relative}: {len(examples)} rows x {len(FEATURE_NAMES)} features")

    # The two training fixtures must be identical: the browser model and the
    # server model are only comparable if they learned from the same data.
    server = (PROJECT_ROOT / "data" / "training.json").read_text(encoding="utf-8")
    browser = (PROJECT_ROOT / "frontend" / "training.json").read_text(encoding="utf-8")
    check(
        json.loads(server) == json.loads(browser),
        "data/training.json and frontend/training.json differ; the server and "
        "browser models would not be comparable",
    )

    sample = json.loads((PROJECT_ROOT / "data" / "sample.json").read_text("utf-8"))
    check("job" in sample and "vendors" in sample, "data/sample.json is malformed")
    check(
        all("sample_size" in vendor for vendor in sample["vendors"]),
        "data/sample.json vendors lack sample_size, so confidence falls back to "
        "the unknown-provenance prior",
    )

    catalog = json.loads((PROJECT_ROOT / "frontend" / "jobs.json").read_text("utf-8"))
    check(bool(catalog.get("scenarios")), "frontend/jobs.json has no scenarios")
    check(bool(catalog.get("vendors")), "frontend/jobs.json has no vendors")
    ids = [scenario["job"]["job_id"] for scenario in catalog["scenarios"]]
    check(len(ids) == len(set(ids)), f"frontend/jobs.json has duplicate job ids: {ids}")
    for scenario in catalog["scenarios"]:
        job = scenario["job"]
        for field in (
            "job_id",
            "customer_name",
            "site_name",
            "asset_label",
            "job_type",
            "title",
            "details",
            "region",
            "sla_hours",
            "risk_level",
        ):
            check(
                field in job and job[field] not in (None, ""),
                f"frontend/jobs.json scenario {scenario.get('id')} is missing {field}",
            )
    print(f"  frontend/jobs.json: {len(ids)} scenarios, {len(catalog['vendors'])} vendors")


# --------------------------------------------------------------------------
# 6. CI must test before it deploys.
# --------------------------------------------------------------------------
def validate_ci() -> None:
    section("CI ordering")
    path = PROJECT_ROOT / ".github" / "workflows" / "deploy.yml"
    text = path.read_text(encoding="utf-8")
    check("pytest" in text, "deploy.yml never runs pytest")
    if "pytest" in text and "sam deploy" in text:
        check(
            text.index("pytest") < text.index("sam deploy"),
            "deploy.yml runs `sam deploy` before pytest",
        )
    check(
        (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").exists(),
        "No pull-request CI workflow exists",
    )
    print("  deploy.yml tests before deploying")


def main() -> int:
    validate_python()
    validate_dashboard(
        "Local admin",
        APP / "static" / "index.html",
        [APP / "static" / "app.js"],
    )
    validate_dashboard(
        "Hybrid GitHub Pages",
        PROJECT_ROOT / "frontend" / "index.html",
        [
            PROJECT_ROOT / "frontend" / "app.js",
            PROJECT_ROOT / "frontend" / "aws-client.js",
            PROJECT_ROOT / "frontend" / "local-ai.js",
        ],
    )
    validate_config_global()
    validate_packaging()
    validate_docs()
    validate_fixtures()
    validate_ci()

    print(f"\n{'-' * 62}")
    if failures:
        print(f"FAILED: {len(failures)} problem(s) across {checks_run} checks\n")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"PASSED: {checks_run} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""B16 boundaries: control plane never imports ML libraries; workloads never unpickle/eval."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ML_ROOTS = ("torch", "numpy", "pyarrow", "safetensors")
TORCH_WORKLOADS = {"torch_common", "pytorch_cifar10", "batch_inference"}
CONTROL_PLANE = (
    "nexa.domain",
    "nexa.scheduler",
    "nexa.api",
    "nexa.application",
    "nexa.coordinator",
    "nexa.worker",
    "nexa.cli",
    "nexa.infrastructure",
)
_PROBE = r"""
import importlib, json, pkgutil, sys

blocked = []
roots = set(sys.argv[1].split(","))

class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in roots:
            blocked.append(name)
            raise ImportError(f"blocked {name}")
        return None

sys.meta_path.insert(0, Blocker())
imported = []
for package_name in sys.argv[2].split(","):
    package = importlib.import_module(package_name)
    imported.append(package_name)
    for module in pkgutil.walk_packages(package.__path__, package_name + "."):
        # Entrypoint modules build an app from the environment at import time.
        if module.name.endswith(("__main__", ".main")):
            continue
        importlib.import_module(module.name)
        imported.append(module.name)
for name in sys.argv[3].split(","):
    importlib.import_module(name)
    imported.append(name)
loaded = sorted(name for name in sys.modules if name.split(".")[0] in roots)
print(json.dumps({"imported": imported, "blocked": blocked, "loaded": loaded}))
"""


def _stdlib_workload_modules() -> list[str]:
    return sorted(
        f"nexa.workloads.{path.stem}"
        for path in (ROOT / "src/nexa/workloads").glob("*.py")
        if path.stem != "__init__" and path.stem not in TORCH_WORKLOADS
    )


def test_control_plane_and_runner_import_without_ml_libraries() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE,
            ",".join(ML_ROOTS),
            ",".join(CONTROL_PLANE),
            ",".join(_stdlib_workload_modules()),
        ],
        cwd=ROOT,
        env={"PYTHONPATH": f"{ROOT / 'src'}", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["blocked"] == []
    assert report["loaded"] == []
    assert "nexa.workloads.trusted_runner" in report["imported"]
    assert "nexa.workloads.training_state" in report["imported"]
    assert "nexa.worker.executor" in report["imported"]
    assert "nexa.api.app" in report["imported"]


def test_ml_libraries_stay_out_of_the_control_plane_lock() -> None:
    lock = (ROOT / "uv.lock").read_text()
    for root in ML_ROOTS:
        assert f'name = "{root}"' not in lock
    project = (ROOT / "pyproject.toml").read_text()
    for root in ML_ROOTS:
        assert f'"{root}' not in project


FORBIDDEN_MODULES = {"pickle", "cPickle", "marshal", "shelve", "dill", "cloudpickle", "joblib"}
FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__"}
FORBIDDEN_ATTRIBUTES = {("torch", "save"), ("torch", "load"), ("np", "load"), ("numpy", "load")}


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [
                alias.name for alias in node.names if alias.name.split(".")[0] in FORBIDDEN_MODULES
            ]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in FORBIDDEN_MODULES:
                found.append(node.module)
            if node.module.split(".")[0] in {"torch", "numpy"}:
                found += [alias.name for alias in node.names if alias.name in {"save", "load"}]
        elif isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name) and function.id in FORBIDDEN_BUILTINS:
                found.append(function.id)
            if (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and (function.value.id, function.attr) in FORBIDDEN_ATTRIBUTES
            ):
                found.append(f"{function.value.id}.{function.attr}")
            found += [keyword.arg for keyword in node.keywords if keyword.arg == "allow_pickle"]
        elif isinstance(node, ast.Attribute) and node.attr in {"load_state_dict", "state_dict"}:
            found.append(node.attr)
    return found


def test_workloads_and_scripts_never_unpickle_or_execute_code() -> None:
    paths = sorted((ROOT / "src/nexa/workloads").glob("*.py")) + sorted(
        (ROOT / "scripts").glob("*.py")
    )
    assert any(path.name == "pytorch_cifar10.py" for path in paths)
    report = {str(path.relative_to(ROOT)): _violations(path) for path in paths}
    assert {name: found for name, found in report.items() if found} == {}


def test_forbidden_call_detector_catches_each_pattern(tmp_path: Path) -> None:
    sample = tmp_path / "bad.py"
    sample.write_text(
        "import pickle\nimport torch\nimport numpy as np\nfrom torch import load\n"
        "torch.save(1, 'x')\ntorch.load('x', weights_only=True)\nnp.load('x', allow_pickle=True)\n"
        "eval('1')\nexec('1')\nmodel.load_state_dict({})\n"
    )
    assert sorted(_violations(sample)) == sorted(
        [
            "pickle",
            "load",
            "torch.save",
            "torch.load",
            "np.load",
            "allow_pickle",
            "eval",
            "exec",
            "load_state_dict",
        ]
    )

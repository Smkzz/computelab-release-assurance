"""Install built wheel into an isolated environment and run outside the checkout."""

from __future__ import annotations

import os
import subprocess
import tempfile
import venv
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    wheels = list((root / "dist").glob("computelab_release_assurance-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Build exactly one project wheel first")
    with tempfile.TemporaryDirectory(prefix="computelab-install-") as directory:
        folder = Path(directory)
        venv.EnvBuilder(with_pip=True).create(folder / "env")
        python = folder / "env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        subprocess.run(
            [str(python), "-m", "pip", "install", str(wheels[0])],
            check=True,
            cwd=folder,
            env=env,
            timeout=240,
        )
        subprocess.run(
            [str(python), "-m", "computelab_release", "demo", "--output", "sample"],
            check=True,
            cwd=folder,
            env=env,
            timeout=60,
        )
        subprocess.run(
            [str(python), "-m", "computelab_release", "verify", "sample/regression"],
            check=True,
            cwd=folder,
            env=env,
            timeout=30,
        )
    print("Isolated wheel install and outside-checkout demo passed")


if __name__ == "__main__":
    main()

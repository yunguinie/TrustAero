"""Run the repository tests without loading unrelated user-site plugins.

Some developer machines have globally installed pytest plugins whose optional
dependencies are broken or incompatible with the project environment. Those
plugins are outside TrustAero's dependency lock and must not make its test
result nondeterministic. This wrapper also keeps temporary files on the E-drive
workspace instead of using a user-profile directory on C:.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    """Invoke pytest with the project's reproducible local defaults."""

    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    # This must be set before importing pytest so entry-point discovery cannot
    # import arbitrary plugins installed in the user's global Python profile.
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    import pytest

    arguments = ["-p", "no:cacheprovider"]
    if not any(item.startswith("--basetemp") for item in sys.argv[1:]):
        base_temp = root / ".test-tmp/pytest"
        # pytest creates the final directory itself, but Windows requires its
        # parent to exist. Keep that parent on E: and outside version control.
        base_temp.parent.mkdir(parents=True, exist_ok=True)
        arguments.append(f"--basetemp={base_temp}")
    arguments.extend(sys.argv[1:])
    return int(pytest.main(arguments))


if __name__ == "__main__":
    raise SystemExit(main())

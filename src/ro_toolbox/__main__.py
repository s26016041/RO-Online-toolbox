"""入口點：python -m ro_toolbox"""

from __future__ import annotations

import sys

from ro_toolbox.app import run


def main() -> int:
    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())

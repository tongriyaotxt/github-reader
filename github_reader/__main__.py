"""允许 `python -m github_reader` 直接运行。"""

from .cli import main

raise SystemExit(main())

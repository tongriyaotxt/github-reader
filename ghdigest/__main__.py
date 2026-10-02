"""允许 `python -m ghdigest` 直接运行。"""

from .cli import main

raise SystemExit(main())

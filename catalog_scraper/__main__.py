"""Allows ``python -m catalog_scraper`` as well as the installed console script.

Both spellings appear in the README because the second only exists after
``pip install -e .`` and readers try the first.
"""

import sys

from catalog_scraper.cli import main

if __name__ == "__main__":
    sys.exit(main())

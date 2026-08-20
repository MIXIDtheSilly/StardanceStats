from __future__ import annotations

import pytest

from src.api.middleware import clear as clear_pages
from src.api.services.counting import clear as clear_counts


@pytest.fixture(autouse=True)
def fresh_caches():
    """A held read outlives the database a test built it from, which the next one drops."""
    clear_pages()
    clear_counts()
    yield
    clear_pages()
    clear_counts()

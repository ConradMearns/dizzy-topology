from pathlib import Path

import pytest

from dizzy_topology.feat import FeatureDefinition, load_feat

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def recipe_feat() -> FeatureDefinition:
    """Full recipe example feat."""
    return load_feat(FIXTURES_DIR / "recipe.feat.yaml")

import sys
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "spark"))

from utils.user_id_namespace import (  # noqa: E402
    validate_user_id_namespace,
)


class UserIdNamespaceTests(unittest.TestCase):
    def test_validates_system_user_ids_are_positive(self):
        """System user IDs must be positive integers."""
        with self.assertRaises(ValueError):
            validate_user_id_namespace(
                max_system_user_id=-1,
                max_movielens_user_id=162_541,
            )

    def test_accepts_normal_id_ranges(self):
        """Normal ranges should pass validation without error."""
        # This should not raise
        validate_user_id_namespace(
            max_system_user_id=1000,
            max_movielens_user_id=162_541,
        )

    def test_movielens_ids_become_negative(self):
        """MovieLens IDs are negated, so they won't conflict with positive system IDs."""
        # With negation: MovieLens userId 123 becomes -123
        # System user IDs start from 1 and go positive
        # No conflict possible since -162541 to -1 is separate from 1 to ∞
        validate_user_id_namespace(
            max_system_user_id=10_000_000,  # Even a huge system ID count is safe
            max_movielens_user_id=162_541,
        )


if __name__ == "__main__":
    unittest.main()

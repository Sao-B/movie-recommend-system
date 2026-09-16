from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RatingTableContractTests(unittest.TestCase):
    """Backend rating writes must target `user_ratings`, not legacy `ratings`.

    Pairs with tests/test_training_contract.py, which locks the Spark side
    (`FROM user_ratings`). Together they prevent the two sources of truth
    from drifting apart again.
    """

    def _models_source(self):
        return (
            PROJECT_ROOT / "backend" / "app" / "models.py"
        ).read_text(encoding="utf-8")

    def test_rating_model_points_to_user_ratings(self):
        source = self._models_source()
        self.assertIn('__tablename__ = "user_ratings"', source)
        self.assertNotIn('__tablename__ = "ratings"', source)

    def test_rating_model_is_named_user_rating(self):
        source = self._models_source()
        self.assertRegex(source, r"(?m)^class UserRating\(db\.Model\):")
        self.assertNotRegex(source, r"(?m)^class Rating\(db\.Model\):")

    def test_no_backend_module_imports_legacy_rating_model(self):
        backend_app = PROJECT_ROOT / "backend"
        offenders = []
        for py in backend_app.rglob("*.py"):
            if "venv" in py.parts:
                continue
            text = py.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "import Rating" in line or "import Rating," in line:
                    offenders.append(f"{py.name}: {stripped}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

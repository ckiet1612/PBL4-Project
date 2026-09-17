import nexa


def test_package_imports_with_bootstrap_version() -> None:
    assert nexa.__version__ == "0.1.0"

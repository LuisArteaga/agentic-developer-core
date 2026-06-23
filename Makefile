.PHONY: verify setup

setup:
	@if ! command -v pre-commit >/dev/null 2>&1; then \
		echo "Installing pre-commit..."; \
		pip install pre-commit || pip install --break-system-packages pre-commit || (echo "Error: Please activate your virtual environment or install pre-commit manually (e.g. 'pipx install pre-commit' or 'sudo apt install pre-commit')" && exit 1); \
	fi
	pre-commit install

verify:
	python3 -m unittest discover -s . -p "test_*.py"

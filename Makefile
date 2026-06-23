.PHONY: verify setup secret-scan

setup:
	@echo "Installing native git pre-commit hook..."
	@echo "#!/bin/sh" > .git/hooks/pre-commit
	@echo "python3 scripts/secret_scan.py --staged" >> .git/hooks/pre-commit
	@chmod +x .git/hooks/pre-commit
	@echo "Git hook setup completed successfully."

secret-scan:
	python3 scripts/secret_scan.py

verify: secret-scan
	python3 -m unittest discover -s . -p "test_*.py"


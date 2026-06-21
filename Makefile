.PHONY: verify test

verify: test

test:
	.venv/bin/python -m unittest discover -s . -p "test_*.py"

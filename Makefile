.PHONY: verify

verify:
	python3 -m unittest discover -s . -p "test_*.py"

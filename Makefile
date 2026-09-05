.PHONY: app install test lint check reset

app:
	streamlit run app.py

install:
	pip install -e '.[dev]'

test:
	pytest -q

lint:
	ruff check .

check: lint test

reset:
	rm -rf .data

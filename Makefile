.PHONY: install run dev clean

install:
	uv pip install --upgrade pip
	uv pip install -r requirements.txt

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000

dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

clean:
	rm -rf __pycache__ .pytest_cache .ruff_cache .venv build dist *.egg-info

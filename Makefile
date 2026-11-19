PY=python3
run:
	PYTHONPATH=src:. $(PY) -m samplepull.cli
run_inc:
	PYTHONPATH=src:. $(PY) -m samplepull.cli --incremental
quality:
	PYTHONPATH=src:. $(PY) -m samplepull.cli --quality
quality_inc:
	PYTHONPATH=src:. $(PY) -m samplepull.cli --quality --incremental
quality_list:
	PYTHONPATH=src:. $(PY) -m samplepull.cli --quality --list
quality_show:
	PYTHONPATH=src:. $(PY) -m samplepull.cli --quality --show $(SAMPLE)
test:
	PYTHONPATH=src:. $(PY) -m pytest -q
mock:
	PYTHONPATH=src:. $(PY) -m uvicorn mockapp.main:app --port 8000
clean:
	rm -rf .pytest_cache **/__pycache__ *.db

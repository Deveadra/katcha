.PHONY: up down logs migrate test lint typecheck dev-api dev-worker

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f api worker

migrate:
	alembic upgrade head

test:
	pytest -q

lint:
	ruff check src tests

typecheck:
	mypy src

dev-api:
	uvicorn katcha.api.main:app --reload

dev-worker:
	python -m katcha.orchestration.worker

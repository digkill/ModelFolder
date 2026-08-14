.PHONY: api web install

install:
	cd backend && python3 -m pip install -r requirements.txt

api:
	cd backend && python3 -m uvicorn app.main:app --host 127.0.0.1 --port 18082 --reload

web:
	cd web && npm run dev

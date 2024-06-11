IMAGE_NAME=tw-reservoir
CONTAINER_NAME=tw-reservoir

OPTIONS:=

up:
	docker start ${CONTAINER_NAME}

stop:
	docker stop ${CONTAINER_NAME}

shell: run
	docker exec -it ${CONTAINER_NAME} /bin/bash

build: venv/bin/activate

server: build
	. venv/bin/activate; python -m main

deploy:
	gcloud app deploy --project='reservoir-358117' --promote --stop-previous-version ${OPTIONS}

update:
	source venv/bin/activate && python app/data.py >> public/reservoir-history/$$(date "+%Y").tsv
	git diff

################################################

venv/bin/activate: requirements.txt
	python -m venv venv
	. venv/bin/activate; pip install -r requirements.txt

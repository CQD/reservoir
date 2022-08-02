IMAGE_NAME=tw-reservoir
CONTAINER_NAME=tw-reservoir

OPTIONS:=

run:
	docker start ${CONTAINER_NAME}

stop:
	docker stop ${CONTAINER_NAME}

shell: run
	docker exec -it ${CONTAINER_NAME} /bin/bash

build:
	docker build -t ${IMAGE_NAME} .
	docker rm ${CONTAINER_NAME} || true
	docker create --name ${CONTAINER_NAME} -v $$(pwd):/app -p 80:80 ${IMAGE_NAME}

deploy:
	gcloud app deploy --project='reservoir-358117' --promote --stop-previous-version ${OPTIONS}

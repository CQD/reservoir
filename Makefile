.PHONY: run build

IMAGE_NAME=cqd-water
CONTAINER_NAME=tw-reservoir

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

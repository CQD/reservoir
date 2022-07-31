.PHONY: run build

IMAGE_NAME=cqd-water

run:
	docker run --rm -v $$(pwd):/app -p 80:80 ${IMAGE_NAME}

build:
	docker build -t ${IMAGE_NAME} .

